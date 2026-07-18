from flask import Flask, request, jsonify, send_from_directory
import sqlite3
import random
import string
import bcrypt
import jwt
import os
import requests
from functools import wraps

# Store OTPs temporarily
otp_store = {}

def send_otp(phone):
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    url = "https://www.fast2sms.com/dev/bulkV2"
    headers = {
        "authorization": os.environ.get('FAST2SMS_API_KEY'),
        "Content-Type": "application/json"
    }
    payload = {
        "route": "otp",
        "variables_values": otp,
        "flash": 0,
        "numbers": phone
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        print("Fast2SMS response:", response.text)
        return True
    except Exception as e:
        print("Fast2SMS error:", str(e))
        return False


app = Flask(__name__)
SECRET = 'vocabsecretkey123'

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

# Register with phone
@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    phone = data['phone']
    password = data['password']
    if not phone or not password:
        return jsonify({'error': 'Phone and password required'}), 400
    if len(phone) != 10 or not phone.isdigit():
        return jsonify({'error': 'Enter valid 10 digit phone number'}), 400
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    try:
        conn = get_db()
        conn.execute('INSERT INTO users (phone, password) VALUES (?, ?)',
                    (phone, hashed))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Registered successfully'})
    except:
        return jsonify({'error': 'Phone number already registered'}), 400

# Login with phone
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    phone = data['phone']
    password = data['password']
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE phone = ?',
                       (phone,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    if not bcrypt.checkpw(password.encode('utf-8'), user['password']):
        return jsonify({'error': 'Invalid password'}), 400
    token = jwt.encode({'id': user['id'], 'phone': phone},
                      SECRET, algorithm='HS256')
    return jsonify({'token': token, 'phone': phone})

@app.route('/api/words', methods=['GET'])
@authenticate
def get_words():
    conn = get_db()
    words = conn.execute('SELECT * FROM words WHERE user_id = ?',
                        (request.user['id'],)).fetchall()
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
    conn.execute('DELETE FROM words WHERE id = ? AND user_id = ?',
                (id, request.user['id']))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Word deleted successfully'})

@app.route('/api/practice', methods=['GET'])
@authenticate
def practice():
    conn = get_db()
    words = conn.execute('SELECT * FROM words WHERE user_id = ?',
                        (request.user['id'],)).fetchall()
    conn.close()
    words_list = [dict(w) for w in words]
    random.shuffle(words_list)
    return jsonify(words_list)

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
    if len(phone) != 10 or not phone.isdigit():
        return jsonify({'error': 'Enter valid 10 digit phone number'}), 400
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
    conn.close()
    if user:
        return jsonify({'error': 'Phone number already registered'}), 400
    
    

    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP sent! (Testing mode - OTP: {otp})'})

# Verify OTP and register
@app.route('/api/verify-register', methods=['POST'])
def verify_register():
    data = request.json
    phone = data['phone']
    otp = data['otp']
    password = data['password']
    if phone not in otp_store or otp_store[phone] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[phone]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    try:
        conn = get_db()
        conn.execute('INSERT INTO users (phone, password) VALUES (?, ?)', (phone, hashed))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Registered successfully'})
    except:
        return jsonify({'error': 'Registration failed'}), 400

# Send OTP for forgot password
@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    phone = data['phone']
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE phone = ?', (phone,)).fetchone()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP sent! (Testing mode - OTP: {otp})'})

# Verify OTP and reset password
@app.route('/api/reset-password', methods=['POST'])
def reset_password():
    data = request.json
    phone = data['phone']
    otp = data['otp']
    password = data['password']
    if phone not in otp_store or otp_store[phone] != otp:
        return jsonify({'error': 'Invalid or expired OTP'}), 400
    del otp_store[phone]
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    conn = get_db()
    conn.execute('UPDATE users SET password = ? WHERE phone = ?', (hashed, phone))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Password reset successfully'})



if __name__ == '__main__':
    app.run(port=5000)