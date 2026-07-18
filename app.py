from flask import Flask, request, jsonify, send_from_directory
import random
import string
import bcrypt
import jwt
import os
import psycopg2
import psycopg2.extras
from functools import wraps

app = Flask(__name__)
SECRET = 'vocabsecretkey123'
otp_store = {}

def get_db():
    conn = psycopg2.connect(os.environ.get('DATABASE_URL'))
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            phone TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS words (
            id SERIAL PRIMARY KEY,
            word TEXT NOT NULL,
            meaning TEXT NOT NULL,
            user_id INTEGER REFERENCES users(id)
        )
    ''')
    conn.commit()
    cur.close()
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

@app.route('/api/send-otp', methods=['POST'])
def send_otp_route():
    data = request.json
    phone = data['phone']
    if len(phone) != 10 or not phone.isdigit():
        return jsonify({'error': 'Enter valid 10 digit phone number'}), 400
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    user = cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user:
        return jsonify({'error': 'Phone number already registered'}), 400
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP (Testing mode): {otp}'})

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
        cur = conn.cursor()
        cur.execute('INSERT INTO users (phone, password) VALUES (%s, %s)', (phone, hashed.decode('utf-8')))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({'message': 'Registered successfully'})
    except Exception as e:
        print("Register error:", str(e))
        return jsonify({'error': 'Phone number already registered'}), 400

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    phone = data['phone']
    password = data['password']
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    if not bcrypt.checkpw(password.encode('utf-8'), user['password'].encode('utf-8')):
        return jsonify({'error': 'Invalid password'}), 400
    token = jwt.encode({'id': user['id'], 'phone': phone}, SECRET, algorithm='HS256')
    return jsonify({'token': token, 'phone': phone})

@app.route('/api/forgot-password', methods=['POST'])
def forgot_password():
    data = request.json
    phone = data['phone']
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM users WHERE phone = %s', (phone,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return jsonify({'error': 'Phone number not registered'}), 400
    otp = ''.join(random.choices(string.digits, k=6))
    otp_store[phone] = otp
    return jsonify({'message': f'OTP (Testing mode): {otp}'})

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
    cur = conn.cursor()
    cur.execute('UPDATE users SET password = %s WHERE phone = %s', (hashed.decode('utf-8'), phone))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Password reset successfully'})

@app.route('/api/words', methods=['GET'])
@authenticate
def get_words():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM words WHERE user_id = %s', (request.user['id'],))
    words = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(w) for w in words])

@app.route('/api/words', methods=['POST'])
@authenticate
def add_word():
    data = request.json
    conn = get_db()
    cur = conn.cursor()
    cur.execute('INSERT INTO words (word, meaning, user_id) VALUES (%s, %s, %s)',
                (data['word'], data['meaning'], request.user['id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Word added successfully'})

@app.route('/api/words/<int:id>', methods=['DELETE'])
@authenticate
def delete_word(id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute('DELETE FROM words WHERE id = %s AND user_id = %s', (id, request.user['id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'message': 'Word deleted successfully'})

@app.route('/api/practice', methods=['GET'])
@authenticate
def practice():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute('SELECT * FROM words WHERE user_id = %s', (request.user['id'],))
    words = cur.fetchall()
    cur.close()
    conn.close()
    words_list = [dict(w) for w in words]
    random.shuffle(words_list)
    return jsonify(words_list)

if __name__ == '__main__':
    app.run(port=5000)