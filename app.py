from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import random
import string
import bcrypt
import jwt
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

app = Flask(__name__)
SECRET = 'vocabsecretkey123'

# Store OTPs temporarily
otp_store = {}

def send_otp_email(email):
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[email] = otp
    try:
        sender_email = os.environ.get('GMAIL_EMAIL')
        sender_password = os.environ.get('GMAIL_PASSWORD')
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = email
        msg['Subject'] = "Your Vocabulary App OTP"
        body = f"Your OTP for Vocabulary App is: {otp}\n\nThis OTP is valid for 10 minutes.\n\nDo not share this OTP with anyone."
        msg.attach(MIMEText(body, 'plain'))
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("Email error:", str(e))
        return False

def get_db():
    conn = sqlite3.connect('vocabulary.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL,
            meaning TEXT NOT NULL,
            user_id INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def authenticate(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not token:
            return jsonify({'error': 'No token'}), 401
        try:
            data = jwt.decode(token, SECRET, algorithms=['HS256'])
            request.user = data
        except:
            return jsonify({'error': 'Invalid token'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def home():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'index.html')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'manifest.json')

@app.route('/service-worker.js')
def service_worker():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'service-worker.js')

@app.route('/icon.png')
def icon():
    return send_from_directory(os.path.join(os.path.dirname(__file__), 'public'), 'icon.png')

# Send OTP for registration
@app.route('/api/send-otp', methods=['POST'])
def send_otp_route():
    data = request.json
    phone = data['phone']
    email = data['email']
    if len(phone) != 10 or not phone.isdigit():
        return jsonify({'error': 'Enter valid 10 digit phone number'}), 400
    if not email or '@' not in email:
        return jsonify({'error': 'Enter valid email address'}), 400
    conn = get_db()
    phone_exists = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
    email_exists = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()
    if phone_exists:
        return jsonify({'error': 'Phone number already registered'}), 400
    if email_exists:
        return jsonify({'error': 'Email already registered'}), 400
    if send_otp_email(email):
        return jsonify({'message': f'OTP sent to {email}! Check your inbox.'})
    else:
        return jsonify({'error': 'Failed to send OTP email'}), 500

# Verify OTP and register
@app.route('/api/verify-register', methods=['POST'])
def verify_register():
    data = request.json
    phone = data['phone']
    email = data['email']
    otp = data['otp']
    password = data['password']
    if email not in otp_store or otp_store[email] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[email]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    try:
        conn = get_db()
        conn.execute('INSERT INTO users (phone, email, password) VALUES (?, ?, ?)',
                    (phone, email, hashed))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Registered successfully'})
    except:
        return jsonify({'error': 'Registration failed'}), 400

# Login with phone
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    phone = data['phone']
    password = data['password']
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    if not bcrypt.checkpw(password.encode('utf-8'), user['password']):
        return jsonify({'error': 'Invalid password'}), 400
    token = jwt.encode({'id': user['id'], 'phone': phone}, SECRET, algorithm='HS256')
    return jsonify({'token': token, 'phone': phone})

# Send OTP for forgot password
@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    email = data['email']
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE email = ?', (email,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'Email not registered'}), 400
    if send_otp_email(email):
        return jsonify({'message': f'OTP sent to {email}! Check your inbox.'})
    else:
        return jsonify({'error': 'Failed to send OTP email'}), 500

# Verify OTP and reset password
@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    email = data['email']
    otp = data['otp']
    password = data['password']
    if email not in otp_store or otp_store[email] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[email]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    conn = get_db()
    conn.execute('UPDATE users SET password = ? WHERE email = ?', (hashed, email))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Password reset successfully'})

@app.route('/api/words', methods=['GET'])
@authenticate
def get_words():
    conn = get_db()
    words = conn.execute('SELECT * FROM words WHERE user_id = ?', (request.user['id'],)).fetchall()
    conn.close()
    return jsonify([dict(w) for w in words])

@app.route('/api/words', methods=['POST'])
@authenticate
def add_word():
    data = request.json
    conn = get_db()
    conn.execute('INSERT INTO words (word, meaning, user_id) VALUES (?, ?, ?)',
                (data['word'], data['meaning'], request.user['id']))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Word added successfully'})

@app.route('/api/words/<int:id>', methods=['DELETE'])
@authenticate
def delete_word(id):
    conn = get_db()
    conn.execute('DELETE FROM words WHERE id = ? AND user_id = ?', (id, request.user['id']))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Word deleted successfully'})

@app.route('/api/practice', methods=['GET'])
@authenticate
def practice():
    conn = get_db()
    words = conn.execute('SELECT * FROM words WHERE user_id = ?', (request.user['id'],)).fetchall()
    conn.close()
    words_list = [dict(w) for w in words]
    random.shuffle(words_list)
    return jsonify(words_list)

if __name__ == '__main__':
    app.run(port=5000)