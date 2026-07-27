import os
import json
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
import razorpay
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY')

# Using SQLite for local development. 
# Swap this with your Neon.db PostgreSQL connection string when deploying.
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///hr_app.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialize external APIs
razorpay_client = razorpay.Client(auth=(os.getenv("RAZORPAY_KEY_ID"), os.getenv("RAZORPAY_KEY_SECRET")))
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# --- DATABASE MODELS ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False)
    password = db.Column(db.String(60), nullable=False)
    is_subscribed = db.Column(db.Boolean, default=False)
    interviews = db.relationship('Interview', backref='candidate', lazy=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

class Interview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    date_taken = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    score = db.Column(db.Integer, nullable=False)
    feedback = db.Column(db.Text, nullable=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- AUTHENTICATION ROUTES ---
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "danger")
            return redirect(url_for('signup'))
            
        user = User(username=username, password=hashed_pw)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('login'))
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and bcrypt.check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash("Login unsuccessful.", "danger")
    return render_template('login.html')

@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- CORE APPLICATION ROUTES ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/dashboard')
@login_required
def dashboard():
    # Fetch only the last 5 interviews for the history limit
    recent_interviews = Interview.query.filter_by(user_id=current_user.id).order_by(Interview.date_taken.desc()).limit(5).all()
    return render_template('dashboard.html', interviews=recent_interviews)

@app.route('/interview')
@login_required
def interview():
    # Enforce limit: 4 free interviews per current month
    if not current_user.is_subscribed:
        first_day_of_month = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        monthly_count = Interview.query.filter(
            Interview.user_id == current_user.id,
            Interview.date_taken >= first_day_of_month
        ).count()
        
        if monthly_count >= 4:
            flash("You have reached your free limit of 4 interviews this month.", "warning")
            return redirect(url_for('subscription'))
            
    return render_template('interview.html')

@app.route('/api/chat', methods=['POST'])
@login_required
def chat_api():
    """Handles the AI HR logic. Expects the user's spoken answer and conversation history."""
    data = request.json
    user_answer = data.get('answer', '')
    history = data.get('history', [])
    
    # Strict JSON formatting instructions for the AI
    system_prompt = """
    You are an expert AI HR Interviewer conducting a behavioral and technical interview for a Software Engineer position.
    Act like a real human HR professional. Ask ONE clear question at a time based on standard tech industry practices.
    If the user has answered 5 questions in total, you MUST end the interview.
    You must respond strictly in JSON format matching this schema:
    {
      "response": "Your spoken response acknowledging their answer, followed by your next question.",
      "is_finished": boolean (true if interview is over, false otherwise),
      "score": integer (evaluate overall performance out of 100. ONLY provide this when is_finished is true, else null),
      "feedback": "A brief constructive feedback paragraph highlighting strengths and areas for improvement. ONLY provide this when is_finished is true, else null"
    }
    """
    
    conversation = "\n".join([f"{msg['role']}: {msg['content']}" for msg in history])
    conversation += f"\nCandidate: {user_answer}"
    
    prompt = f"{system_prompt}\n\nConversation so far:\n{conversation}\n\nHR Response (in JSON):"
    
    model = genai.GenerativeModel('gemini-1.5-flash')
    try:
        response = model.generate_content(prompt)
        raw_text = response.text.strip().replace('```json', '').replace('```', '')
        ai_data = json.loads(raw_text)
        
        # If the AI dictates the interview is over, save the score and feedback to the database
        if ai_data.get('is_finished') and ai_data.get('score'):
            new_interview = Interview(
                user_id=current_user.id,
                score=ai_data['score'],
                feedback=ai_data['feedback']
            )
            db.session.add(new_interview)
            db.session.commit()
            
        return jsonify(ai_data)
    except Exception as e:
        print("AI parsing error:", e)
        return jsonify({"response": "I had a moment of technical difficulty parsing your answer. Could you please rephrase?", "is_finished": False})

# --- SUBSCRIPTION & PAYMENT ROUTES ---
@app.route('/subscription')
@login_required
def subscription():
    return render_template('subscription.html', key_id=os.getenv("RAZORPAY_KEY_ID"))

@app.route('/create_order', methods=['POST'])
@login_required
def create_order():
    # Create an order for ₹499 (amount is in paise)
    data = {
        "amount": 49900, 
        "currency": "INR",
        "receipt": f"receipt_{current_user.id}_{int(datetime.utcnow().timestamp())}"
    }
    order = razorpay_client.order.create(data=data)
    return jsonify(order)

@app.route('/verify_payment', methods=['POST'])
@login_required
def verify_payment():
    data = request.json
    try:
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': data['razorpay_order_id'],
            'razorpay_payment_id': data['razorpay_payment_id'],
            'razorpay_signature': data['razorpay_signature']
        })
        # Upgrade user upon successful signature verification
        current_user.is_subscribed = True
        db.session.commit()
        return jsonify({"success": True})
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"success": False}), 400

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)