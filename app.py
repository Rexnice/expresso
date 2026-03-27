from flask import Flask, render_template, request, jsonify
import psycopg2
import psycopg2.extras
import os
import json
import random
import string
import qrcode
import io
import base64
import threading
from datetime import datetime
from dotenv import load_dotenv
import hashlib
import requests
import time
import hmac

load_dotenv()

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.secret_key = os.environ.get('SECRET_KEY')

DATABASE_URL = os.environ.get('DATABASE_URL')
ABLY_API_KEY = os.environ.get('ABLY_API_KEY')

QUESTION_TIME = 20
REVEAL_TIME = 6

# ====================== DATABASE ======================
def get_db():
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        conn.autocommit = True
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}")
        return None

def init_db():
    conn = get_db()
    if not conn:
        return
    
    try:
        cur = conn.cursor()
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS questions (
                id SERIAL PRIMARY KEY,
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                correct_answer CHAR(1) NOT NULL,
                category VARCHAR(50) DEFAULT 'General',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id SERIAL PRIMARY KEY,
                room_code VARCHAR(6) UNIQUE NOT NULL,
                status VARCHAR(20) DEFAULT 'waiting',
                current_question INT DEFAULT 0,
                question_ids JSONB DEFAULT '[]',
                game_state JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS players (
                id SERIAL PRIMARY KEY,
                game_id INT REFERENCES games(id),
                name VARCHAR(50) NOT NULL,
                player_token VARCHAR(100) UNIQUE,
                score INT DEFAULT 0,
                joined_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS answers (
                id SERIAL PRIMARY KEY,
                game_id INT REFERENCES games(id),
                player_id INT REFERENCES players(id),
                question_id INT REFERENCES questions(id),
                answer CHAR(1),
                is_correct BOOLEAN,
                response_time_ms INT,
                points_earned INT DEFAULT 0,
                answered_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        cur.execute("SELECT COUNT(*) FROM questions")
        if cur.fetchone()[0] == 0:
            insert_default_questions(cur)
        
        cur.close()
        conn.close()
        print("✅ Database initialized")
    except Exception as e:
        print(f"❌ DB init error: {e}")

def insert_default_questions(cur):
    questions = [
        ("What is the capital of France?", "London", "Berlin", "Paris", "Madrid", "C", "Geography"),
        ("Who painted the Mona Lisa?", "Van Gogh", "Picasso", "Da Vinci", "Rembrandt", "C", "Art"),
        ("What is 2 + 2?", "3", "4", "5", "6", "B", "Math"),
        ("Which planet is known as the Red Planet?", "Venus", "Mars", "Jupiter", "Saturn", "B", "Science"),
        ("Who wrote Romeo and Juliet?", "Charles Dickens", "Mark Twain", "William Shakespeare", "Jane Austen", "C", "Literature"),
        ("What is the largest ocean?", "Atlantic", "Indian", "Arctic", "Pacific", "D", "Geography"),
        ("Who discovered gravity?", "Einstein", "Newton", "Galileo", "Tesla", "B", "Science"),
        ("What is the square root of 64?", "6", "7", "8", "9", "C", "Math"),
    ]
    cur.executemany("""
        INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_answer, category)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, questions)

# ====================== ABLY ======================
def _publish_to_ably(channel, name, data):
    if not ABLY_API_KEY:
        return
    try:
        key_name, key_secret = ABLY_API_KEY.split(':')
        auth = base64.b64encode(f"{key_name}:{key_secret}".encode()).decode()
        url = f"https://rest.ably.io/channels/{channel}/messages"
        requests.post(
            url,
            headers={'Authorization': f'Basic {auth}', 'Content-Type': 'application/json'},
            json={'name': name, 'data': data},
            timeout=5
        )
    except Exception as e:
        print(f"❌ Ably error: {e}")

@app.route('/api/ably-token', methods=['POST'])
def get_ably_token():
    try:
        key_name, key_secret = ABLY_API_KEY.split(':')
        client_id = request.json.get('player_name', 'guest')
        ttl = 3600000
        capability = json.dumps({"*": ["publish", "subscribe"]})
        timestamp = int(time.time() * 1000)
        nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))
        sign_text = f"{key_name}\n{ttl}\n{capability}\n{client_id}\n{timestamp}\n{nonce}"
        mac = hmac.new(key_secret.encode(), sign_text.encode(), hashlib.sha256).digest()
        token = {
            "keyName": key_name,
            "ttl": ttl,
            "capability": capability,
            "clientId": client_id,
            "timestamp": timestamp,
            "nonce": nonce,
            "mac": base64.b64encode(mac).decode()
        }
        return jsonify(token)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ====================== HELPERS ======================
def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

def generate_qr_code(url):
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a0a00", back_color="#fff8f0")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()

def get_game_state(room_code):
    conn = get_db()
    if not conn:
        return None
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM games WHERE room_code = %s", (room_code,))
    game = cur.fetchone()
    cur.close()
    conn.close()
    return game

def save_game_state(room_code, game_state):
    conn = get_db()
    if not conn:
        return
    cur = conn.cursor()
    
    # Convert any non-serializable objects
    game_state_copy = {}
    for key, value in game_state.items():
        if isinstance(value, datetime):
            game_state_copy[key] = value.isoformat()
        elif isinstance(value, dict):
            # Recursively clean nested dicts
            cleaned = {}
            for k, v in value.items():
                if isinstance(v, datetime):
                    cleaned[k] = v.isoformat()
                else:
                    cleaned[k] = v
            game_state_copy[key] = cleaned
        else:
            game_state_copy[key] = value
    
    cur.execute(
        "UPDATE games SET game_state = %s WHERE room_code = %s",
        (json.dumps(game_state_copy), room_code)
    )
    cur.close()
    conn.close()

def get_players(room_code):
    conn = get_db()
    if not conn:
        return []
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT p.* FROM players p
        JOIN games g ON g.id = p.game_id
        WHERE g.room_code = %s
        ORDER BY p.score DESC
    """, (room_code,))
    players = cur.fetchall()
    cur.close()
    conn.close()
    return players

def save_player_answer(game_id, player_id, question_id, answer, is_correct, elapsed_ms, points):
    conn = get_db()
    if not conn:
        return
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO answers (game_id, player_id, question_id, answer, is_correct, response_time_ms, points_earned)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (game_id, player_id, question_id, answer, is_correct, elapsed_ms, points))
    cur.close()
    conn.close()

def _send_question(room_code):
    game = get_game_state(room_code)
    if not game or game['status'] != 'active':
        return
    
    game_state = game['game_state'] if game['game_state'] else {}
    q_index = game_state.get('current_question', 0)
    question_ids = game['question_ids']
    
    if q_index >= len(question_ids):
        _end_game(room_code)
        return
    
    conn = get_db()
    if not conn:
        return
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM questions WHERE id = %s", (question_ids[q_index],))
    q = dict(cur.fetchone())
    cur.close()
    conn.close()
    
    # Remove datetime fields from question data for JSON serialization
    clean_q = {
        'id': q['id'],
        'question': q['question'],
        'option_a': q['option_a'],
        'option_b': q['option_b'],
        'option_c': q['option_c'],
        'option_d': q['option_d'],
        'correct_answer': q['correct_answer'],
        'category': q.get('category', 'General')
    }
    
    game_state['current_question_data'] = clean_q
    game_state['current_answers'] = {}
    game_state['question_start_time'] = datetime.now().timestamp() * 1000
    game_state['current_question_index'] = q_index
    save_game_state(room_code, game_state)
    
    payload = {
        'question_number': q_index + 1,
        'total_questions': len(question_ids),
        'question': q['question'],
        'options': {'A': q['option_a'], 'B': q['option_b'], 'C': q['option_c'], 'D': q['option_d']},
        'category': q.get('category', 'General'),
        'time_limit': QUESTION_TIME
    }
    
    _publish_to_ably(f"game:{room_code}", 'new_question', payload)
    print(f"📢 Question {q_index + 1} sent to room {room_code}")
    
    timer = threading.Timer(QUESTION_TIME, lambda: _auto_reveal(room_code))
    timer.daemon = True
    timer.start()

def _auto_reveal(room_code):
    game = get_game_state(room_code)
    if not game or game['status'] != 'active':
        return
    
    game_state = game['game_state'] if game['game_state'] else {}
    q = game_state.get('current_question_data')
    if not q:
        return
    
    counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    for answer in game_state.get('current_answers', {}).values():
        counts[answer['answer']] = counts.get(answer['answer'], 0) + 1
    
    players = get_players(room_code)
    players_sorted = sorted(players, key=lambda x: x['score'], reverse=True)
    
    payload = {
        'correct_answer': q['correct_answer'],
        'answer_counts': counts,
        'leaderboard': [{'name': p['name'], 'score': p['score']} for p in players_sorted[:10]],
        'next_in': REVEAL_TIME
    }
    
    _publish_to_ably(f"game:{room_code}", 'answer_reveal', payload)
    print(f"📊 Answers revealed for room {room_code}")
    
    timer = threading.Timer(REVEAL_TIME, lambda: _advance_question(room_code))
    timer.daemon = True
    timer.start()

def _advance_question(room_code):
    game = get_game_state(room_code)
    if not game or game['status'] != 'active':
        return
    
    game_state = game['game_state'] if game['game_state'] else {}
    game_state['current_question'] = game_state.get('current_question', 0) + 1
    save_game_state(room_code, game_state)
    print(f"⏩ Advancing to question {game_state['current_question'] + 1} in room {room_code}")
    _send_question(room_code)

def _end_game(room_code):
    conn = get_db()
    if not conn:
        return
    cur = conn.cursor()
    cur.execute("UPDATE games SET status = 'ended' WHERE room_code = %s", (room_code,))
    cur.close()
    conn.close()
    
    players = get_players(room_code)
    players_sorted = sorted(players, key=lambda x: x['score'], reverse=True)
    
    payload = {
        'top3': [{'name': p['name'], 'score': p['score']} for p in players_sorted[:3]],
        'all_players': [{'name': p['name'], 'score': p['score']} for p in players_sorted],
    }
    
    _publish_to_ably(f"game:{room_code}", 'game_over', payload)
    print(f"🏁 Game ended in room {room_code}")

# ====================== ROUTES ======================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/host')
def host():
    return render_template('host.html')

@app.route('/play')
def play():
    return render_template('play.html')

@app.route('/join/<room_code>')
def join_via_link(room_code):
    return render_template('play.html', room_code=room_code)

@app.route('/api/create-game', methods=['POST'])
def create_game():
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Database connection failed'}), 500
    
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM questions ORDER BY RANDOM() LIMIT 10")
    ids = [r['id'] for r in cur.fetchall()]
    
    room_code = generate_room_code()
    
    cur.execute("""
        INSERT INTO games (room_code, question_ids, game_state, status) 
        VALUES (%s, %s, %s, 'waiting') RETURNING id
    """, (room_code, json.dumps(ids), json.dumps({'current_question': 0})))
    game_id = cur.fetchone()['id']
    cur.close()
    conn.close()
    
    join_url = f"{request.host_url}join/{room_code}"
    qr = generate_qr_code(join_url)
    
    return jsonify({
        'room_code': room_code,
        'join_url': join_url,
        'qr_code': qr
    })

@app.route('/api/join-game', methods=['POST'])
def join_game():
    try:
        data = request.json
        room_code = data.get('room_code')
        name = data.get('name')
        
        print(f"🔵 Player joining room {room_code} with name {name}")
        
        game = get_game_state(room_code)
        if not game:
            print(f"❌ Game not found: {room_code}")
            return jsonify({'error': 'Game not found'}), 404
        
        if game['status'] != 'waiting':
            print(f"❌ Game already started: {game['status']}")
            return jsonify({'error': 'Game already started'}), 400
        
        players = get_players(room_code)
        for p in players:
            if p['name'].lower() == name.lower():
                print(f"❌ Name already taken: {name}")
                return jsonify({'error': 'Name already taken'}), 400
        
        token = hashlib.md5(f"{name}{datetime.now()}{random.random()}".encode()).hexdigest()[:12]
        
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database error'}), 500
        
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO players (game_id, name, player_token, score) 
            VALUES (%s, %s, %s, %s) RETURNING id
        """, (game['id'], name, token, 0))
        player_id = cur.fetchone()[0]
        cur.close()
        conn.close()
        
        print(f"✅ Player {name} joined room {room_code} with token {token}")
        
        players_list = get_players(room_code)
        _publish_to_ably(f"host:{room_code}", 'player_list_update', {
            'players': [{'name': p['name'], 'score': p['score']} for p in players_list]
        })
        
        return jsonify({'player_token': token})
    except Exception as e:
        print(f"❌ Join error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/game/<room_code>/players', methods=['GET'])
def get_game_players(room_code):
    players = get_players(room_code)
    return jsonify({'players': [{'name': p['name'], 'score': p['score']} for p in players]})

@app.route('/api/start-game', methods=['POST'])
def start_game():
    room_code = request.json.get('room_code')
    
    print(f"🔵 Starting game in room {room_code}")
    
    game = get_game_state(room_code)
    if not game:
        return jsonify({'error': 'Game not found'}), 404
    
    players = get_players(room_code)
    if len(players) < 1:
        return jsonify({'error': 'Need at least 1 player'}), 400
    
    if game['status'] != 'waiting':
        return jsonify({'error': 'Game already started'}), 400
    
    conn = get_db()
    if not conn:
        return jsonify({'error': 'Database error'}), 500
    
    cur = conn.cursor()
    cur.execute("UPDATE games SET status = 'active' WHERE room_code = %s", (room_code,))
    cur.close()
    conn.close()
    
    _publish_to_ably(f"game:{room_code}", 'game_started', {})
    print(f"✅ Game started in room {room_code} with {len(players)} players")
    
    timer = threading.Timer(1, lambda: _send_question(room_code))
    timer.daemon = True
    timer.start()
    
    return jsonify({'success': True})

@app.route('/api/submit-answer', methods=['POST'])
def submit_answer():
    try:
        data = request.json
        room_code = data.get('room_code')
        answer = data.get('answer', '').upper()
        player_token = data.get('player_token')
        
        game = get_game_state(room_code)
        if not game:
            return jsonify({'error': 'Game not found'}), 404
        
        if game['status'] != 'active':
            return jsonify({'error': 'Game not active'}), 400
        
        game_state = game['game_state'] if game['game_state'] else {}
        
        conn = get_db()
        if not conn:
            return jsonify({'error': 'Database error'}), 500
        
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM players WHERE player_token = %s AND game_id = %s", 
                   (player_token, game['id']))
        player = cur.fetchone()
        cur.close()
        
        if not player:
            return jsonify({'error': 'Player not found'}), 404
        
        current_answers = game_state.get('current_answers', {})
        if player_token in current_answers:
            return jsonify({'error': 'Already answered'}), 400
        
        question_start_time = game_state.get('question_start_time')
        if not question_start_time:
            return jsonify({'error': 'No active question'}), 400
        
        q = game_state.get('current_question_data')
        if not q:
            return jsonify({'error': 'No active question'}), 400
        
        elapsed_ms = (datetime.now().timestamp() * 1000) - question_start_time
        
        if elapsed_ms > (QUESTION_TIME * 1000):
            return jsonify({'error': 'Time expired'}), 400
        
        correct = q['correct_answer'].upper()
        is_correct = answer == correct
        
        if is_correct:
            time_factor = max(0, 1 - (elapsed_ms / (QUESTION_TIME * 1000)))
            points = int(500 + 500 * time_factor)
        else:
            points = 0
        
        current_answers[player_token] = {
            'answer': answer,
            'is_correct': is_correct,
            'points': points
        }
        game_state['current_answers'] = current_answers
        save_game_state(room_code, game_state)
        
        cur = conn.cursor()
        cur.execute("UPDATE players SET score = score + %s WHERE id = %s", (points, player['id']))
        cur.close()
        
        save_player_answer(game['id'], player['id'], q['id'], answer, is_correct, int(elapsed_ms), points)
        conn.close()
        
        print(f"✅ Answer from {player['name']} in room {room_code}: {answer} (correct: {is_correct})")
        
        _publish_to_ably(f"game:{room_code}", 'answer_received', {
            'player_token': player_token,
            'is_correct': is_correct,
            'points': points,
            'correct_answer': correct
        })
        
        players_list = get_players(room_code)
        _publish_to_ably(f"host:{room_code}", 'answer_count', {
            'answered': len(current_answers),
            'total': len(players_list)
        })
        
        return jsonify({
            'success': True,
            'is_correct': is_correct,
            'points': points,
            'correct_answer': correct
        })
    except Exception as e:
        print(f"❌ Submit answer error: {e}")
        return jsonify({'error': str(e)}), 500

# ====================== MAIN ======================
init_db()
app.debug = False
application = app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
















# from flask import Flask, render_template, request, jsonify
# from flask_cors import CORS
# import psycopg2
# import psycopg2.extras
# import os
# import json
# import random
# import string
# import qrcode
# import io
# import base64
# import threading
# from datetime import datetime
# from dotenv import load_dotenv
# import hashlib
# import requests
# import time
# import hmac

# load_dotenv()

# app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
# CORS(app)
# app.secret_key = os.environ.get('SECRET_KEY')

# DATABASE_URL = os.environ.get('DATABASE_URL')
# ABLY_API_KEY = os.environ.get('ABLY_API_KEY')

# QUESTION_TIME = 20
# REVEAL_TIME = 6

# # For Vercel, we need to store active games in memory (will be lost on cold starts)
# # For production, use Redis or a database
# active_games = {}

# # ====================== DATABASE ======================
# def get_db():
#     conn = psycopg2.connect(DATABASE_URL)
#     conn.autocommit = True
#     return conn

# def init_db():
#     """Initialize database tables if they don't exist"""
#     try:
#         conn = get_db()
#         cur = conn.cursor()
        
#         # Create questions table
#         cur.execute("""
#             CREATE TABLE IF NOT EXISTS questions (
#                 id SERIAL PRIMARY KEY,
#                 question TEXT NOT NULL,
#                 option_a TEXT NOT NULL,
#                 option_b TEXT NOT NULL,
#                 option_c TEXT NOT NULL,
#                 option_d TEXT NOT NULL,
#                 correct_answer CHAR(1) NOT NULL,
#                 category VARCHAR(50) DEFAULT 'General',
#                 created_at TIMESTAMP DEFAULT NOW()
#             )
#         """)
        
#         # Create games table
#         cur.execute("""
#             CREATE TABLE IF NOT EXISTS games (
#                 id SERIAL PRIMARY KEY,
#                 room_code VARCHAR(6) UNIQUE NOT NULL,
#                 status VARCHAR(20) DEFAULT 'waiting',
#                 current_question INT DEFAULT 0,
#                 question_ids JSONB DEFAULT '[]',
#                 created_at TIMESTAMP DEFAULT NOW()
#             )
#         """)
        
#         # Create players table
#         cur.execute("""
#             CREATE TABLE IF NOT EXISTS players (
#                 id SERIAL PRIMARY KEY,
#                 game_id INT REFERENCES games(id),
#                 name VARCHAR(50) NOT NULL,
#                 player_token VARCHAR(100) UNIQUE,
#                 score INT DEFAULT 0,
#                 joined_at TIMESTAMP DEFAULT NOW()
#             )
#         """)
        
#         # Create answers table
#         cur.execute("""
#             CREATE TABLE IF NOT EXISTS answers (
#                 id SERIAL PRIMARY KEY,
#                 game_id INT REFERENCES games(id),
#                 player_id INT REFERENCES players(id),
#                 question_id INT REFERENCES questions(id),
#                 answer CHAR(1),
#                 is_correct BOOLEAN,
#                 response_time_ms INT,
#                 points_earned INT DEFAULT 0,
#                 answered_at TIMESTAMP DEFAULT NOW()
#             )
#         """)
        
#         # Insert default questions if none exist
#         cur.execute("SELECT COUNT(*) FROM questions")
#         if cur.fetchone()[0] == 0:
#             insert_default_questions(cur)
#             print("✅ Default questions inserted")
        
#         cur.close()
#         conn.close()
#         print("✅ Database initialized successfully")
#     except Exception as e:
#         print(f"⚠️ Database init warning: {e}")

# def insert_default_questions(cur):
#     questions = [
#         ("Which prophet was thrown into a den of lions?", "Jeremiah", "Daniel", "Elijah", "Elisha", "B", "Bible"),
#         ("What is the traditional Yoruba male attire called?", "Kente", "Smock", "Agbada", "Dashiki", "C", "Culture"),
#         ("In Ghana, which soup pairs with Banku and Fufu?", "Pepper soup", "Groundnut soup", "Ogbono soup", "Palm nut soup", "D", "Culture"),
#         ("Which disciple asked to walk on water with Jesus?", "John", "James", "Andrew", "Peter", "D", "Bible"),
#         ("What Nigerian snack is made from beans and fried in oil?", "Puff-puff", "Moi moi", "Akara", "Chin Chin", "C", "Culture"),
#         ("Where did Jesus perform His first miracle (water to wine)?", "Nazareth", "Jerusalem", "Cana", "Bethlehem", "C", "Bible"),
#         ("The traditional Kente cloth originates from which ethnic group?", "Yoruba", "Hausa", "Igbo", "Ashanti", "D", "Culture"),
#         ("What did God provide for the Israelites in the wilderness?", "Quail", "Water", "Manna", "Figs", "C", "Bible"),
#         ("Which Nigerian dish is made from pounded cassava?", "Amala", "Eba", "Fufu", "Semo", "B", "Culture"),
#         ("Who was Paul's companion on his first missionary journey?", "Silas", "Timothy", "Barnabas", "Luke", "C", "Bible"),
#     ]
#     cur.executemany("""
#         INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_answer, category)
#         VALUES (%s, %s, %s, %s, %s, %s, %s)
#     """, questions)

# # ====================== ABLY ======================
# def _publish_to_ably(channel, name, data):
#     if not ABLY_API_KEY:
#         print("❌ Missing ABLY_API_KEY")
#         return
#     try:
#         key_name, key_secret = ABLY_API_KEY.split(':')
#         auth = base64.b64encode(f"{key_name}:{key_secret}".encode()).decode()

#         url = f"https://rest.ably.io/channels/{channel}/messages"

#         res = requests.post(
#             url,
#             headers={
#                 'Authorization': f'Basic {auth}',
#                 'Content-Type': 'application/json'
#             },
#             json={'name': name, 'data': data},
#             timeout=5
#         )

#         if res.status_code not in [200, 201]:
#             print(f"❌ Ably publish failed: {res.text}")
#         else:
#             print(f"📡 Sent → {channel} | {name}")

#     except Exception as e:
#         print(f"❌ Ably error {channel} {name}: {e}")

# @app.route('/api/ably-token', methods=['POST'])
# def get_ably_token():
#     try:
#         key_name, key_secret = ABLY_API_KEY.split(':')

#         client_id = request.json.get('player_name', 'guest')

#         ttl = 3600000
#         capability = json.dumps({"*": ["publish", "subscribe"]})
#         timestamp = int(time.time() * 1000)
#         nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

#         sign_text = f"{key_name}\n{ttl}\n{capability}\n{client_id}\n{timestamp}\n{nonce}"

#         mac = hmac.new(
#             key_secret.encode(),
#             sign_text.encode(),
#             hashlib.sha256
#         ).digest()

#         token = {
#             "keyName": key_name,
#             "ttl": ttl,
#             "capability": capability,
#             "clientId": client_id,
#             "timestamp": timestamp,
#             "nonce": nonce,
#             "mac": base64.b64encode(mac).decode()
#         }

#         return jsonify(token)

#     except Exception as e:
#         print(f"❌ Token error: {e}")
#         return jsonify({'error': str(e)}), 500

# # ====================== HELPERS ======================
# def generate_room_code():
#     return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# def generate_qr_code(url):
#     qr = qrcode.QRCode(version=1, box_size=10, border=4)
#     qr.add_data(url)
#     qr.make(fit=True)
#     img = qr.make_image(fill_color="#1a0a00", back_color="#fff8f0")
#     buf = io.BytesIO()
#     img.save(buf, format='PNG')
#     buf.seek(0)
#     return base64.b64encode(buf.read()).decode()

# # ====================== GAME LOGIC ======================
# def _send_question(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return

#     q_index = game.get('current_question', 0)
#     if q_index >= len(game['question_ids']):
#         _end_game(room_code)
#         return

#     print(f"🔥 Sending question {q_index + 1}")

#     conn = get_db()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute("SELECT * FROM questions WHERE id = %s", (game['question_ids'][q_index],))
#     q = dict(cur.fetchone())
#     cur.close()
#     conn.close()

#     game['current_question_data'] = q
#     game['current_answers'] = {}
#     game['question_start_time'] = datetime.now().timestamp() * 1000

#     payload = {
#         'question_number': q_index + 1,
#         'total_questions': len(game['question_ids']),
#         'question': q['question'],
#         'options': {
#             'A': q['option_a'],
#             'B': q['option_b'],
#             'C': q['option_c'],
#             'D': q['option_d']
#         },
#         'category': q.get('category', 'General'),
#         'time_limit': QUESTION_TIME
#     }

#     _publish_to_ably(f"game:{room_code}", 'new_question', payload)

#     # For Vercel, we need to handle timers carefully
#     timer = threading.Timer(QUESTION_TIME, lambda: _auto_reveal(room_code))
#     timer.daemon = True
#     timer.start()

# def _auto_reveal(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return

#     print(f"🔥 Revealing answers for room {room_code}")

#     q = game['current_question_data']
    
#     # Count answers
#     counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
#     for answer in game['current_answers'].values():
#         counts[answer['answer']] = counts.get(answer['answer'], 0) + 1

#     players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)

#     payload = {
#         'correct_answer': q['correct_answer'],
#         'answer_counts': counts,
#         'leaderboard': [
#             {'name': p['name'], 'score': p['score']}
#             for p in players_sorted[:10]
#         ],
#         'next_in': REVEAL_TIME
#     }

#     _publish_to_ably(f"game:{room_code}", 'answer_reveal', payload)

#     timer = threading.Timer(REVEAL_TIME, lambda: _advance_question(room_code))
#     timer.daemon = True
#     timer.start()

# def _advance_question(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return
#     game['current_question'] += 1
#     print(f"🔵 Advancing to question {game['current_question'] + 1}")
#     _send_question(room_code)

# def _end_game(room_code):
#     game = active_games.get(room_code)
#     if not game:
#         return

#     game['status'] = 'ended'
#     print(f"✅ Game ended in room {room_code}")

#     players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)
    
#     payload = {
#         'top3': [{'name': p['name'], 'score': p['score']} for p in players_sorted[:3]],
#         'all_players': [{'name': p['name'], 'score': p['score']} for p in players_sorted],
#     }

#     _publish_to_ably(f"game:{room_code}", 'game_over', payload)

# # ====================== ROUTES ======================
# @app.route('/')
# def index():
#     return render_template('index.html')

# @app.route('/host')
# def host():
#     return render_template('host.html')

# @app.route('/play')
# def play():
#     return render_template('play.html')

# @app.route('/join/<room_code>')
# def join_via_link(room_code):
#     return render_template('play.html', room_code=room_code)

# @app.route('/api/create-game', methods=['POST'])
# def create_game():
#     conn = get_db()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

#     cur.execute("SELECT id FROM questions ORDER BY RANDOM() LIMIT 10")
#     ids = [r['id'] for r in cur.fetchall()]

#     room_code = generate_room_code()

#     cur.execute("INSERT INTO games (room_code, question_ids) VALUES (%s, %s) RETURNING id",
#                 (room_code, json.dumps(ids)))
#     game_id = cur.fetchone()['id']

#     cur.close()
#     conn.close()

#     active_games[room_code] = {
#         'game_id': game_id,
#         'room_code': room_code,
#         'status': 'waiting',
#         'current_question': 0,
#         'question_ids': ids,
#         'players': {},
#         'current_answers': {},
#         'current_question_data': None,
#         'question_start_time': None
#     }

#     join_url = f"{request.host_url}join/{room_code}"
#     qr = generate_qr_code(join_url)

#     return jsonify({
#         'room_code': room_code,
#         'join_url': join_url,
#         'qr_code': qr
#     })

# @app.route('/api/join-game', methods=['POST'])
# def join_game():
#     data = request.json
#     room_code = data.get('room_code')
#     name = data.get('name')

#     game = active_games.get(room_code)
#     if not game:
#         return jsonify({'error': 'Invalid room'}), 400

#     # Check for duplicate names
#     for p in game['players'].values():
#         if p['name'].lower() == name.lower():
#             return jsonify({'error': 'Name already taken!'}), 400

#     token = hashlib.md5(f"{name}{datetime.now()}".encode()).hexdigest()[:12]

#     # Save to database
#     try:
#         conn = get_db()
#         cur = conn.cursor()
#         cur.execute("""
#             INSERT INTO players (game_id, name, player_token, score) 
#             VALUES (%s, %s, %s, %s) RETURNING id
#         """, (game['game_id'], name, token, 0))
#         player_id = cur.fetchone()[0]
#         cur.close()
#         conn.close()
#     except Exception as e:
#         print(f"Database error: {e}")
#         # Continue anyway - memory state is primary

#     game['players'][token] = {
#         'player_id': player_id if 'player_id' in locals() else None,
#         'name': name,
#         'score': 0,
#         'token': token
#     }

#     _publish_to_ably(f"host:{room_code}", 'player_list_update', {
#         'players': [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
#     })

#     return jsonify({'player_token': token})

# @app.route('/api/game/<room_code>/players', methods=['GET'])
# def get_game_players(room_code):
#     if room_code not in active_games:
#         return jsonify({'players': []})
    
#     game = active_games[room_code]
#     players = [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
#     return jsonify({'players': players})

# @app.route('/api/start-game', methods=['POST'])
# def start_game():
#     room_code = request.json.get('room_code')
#     game = active_games.get(room_code)

#     if not game:
#         return jsonify({'error': 'Game not found'}), 404
    
#     if len(game['players']) < 1:
#         return jsonify({'error': 'Need at least 1 player!'}), 400

#     if game['status'] != 'waiting':
#         return jsonify({'error': 'Game already started!'}), 400

#     game['status'] = 'active'

#     _publish_to_ably(f"game:{room_code}", 'game_started', {})
    
#     # Small delay to ensure clients are ready
#     timer = threading.Timer(1, lambda: _send_question(room_code))
#     timer.daemon = True
#     timer.start()

#     return jsonify({'success': True})

# @app.route('/api/submit-answer', methods=['POST'])
# def submit_answer():
#     try:
#         data = request.json
#         print(f"🔵 SUBMIT ANSWER: {data}")
        
#         room_code = data.get('room_code')
#         answer = data.get('answer', '').upper()
#         player_token = data.get('player_token')
        
#         if room_code not in active_games:
#             return jsonify({'error': 'Game not found!'}), 404
        
#         game = active_games[room_code]
        
#         if game['status'] != 'active':
#             return jsonify({'error': 'Game not active!'}), 400
        
#         if player_token not in game['players']:
#             return jsonify({'error': 'Player not found!'}), 404
        
#         if player_token in game['current_answers']:
#             return jsonify({'error': 'Already answered!'}), 400
        
#         if game['question_start_time'] is None:
#             return jsonify({'error': 'No active question!'}), 400
        
#         player = game['players'][player_token]
#         q = game['current_question_data']
        
#         if not q:
#             return jsonify({'error': 'No active question!'}), 400
        
#         elapsed_ms = (datetime.now().timestamp() * 1000) - game['question_start_time']
        
#         if elapsed_ms > (QUESTION_TIME * 1000):
#             return jsonify({'error': 'Time expired!'}), 400
        
#         correct = q['correct_answer'].upper()
#         is_correct = answer == correct
        
#         if is_correct:
#             time_factor = max(0, 1 - (elapsed_ms / (QUESTION_TIME * 1000)))
#             points = int(500 + 500 * time_factor)
#         else:
#             points = 0
        
#         game['current_answers'][player_token] = {
#             'answer': answer,
#             'is_correct': is_correct,
#             'points': points
#         }
#         player['score'] += points
        
#         # Try to save to database, but don't fail if it doesn't work
#         try:
#             conn = get_db()
#             cur = conn.cursor()
#             cur.execute("""
#                 INSERT INTO answers (game_id, player_id, question_id, answer, is_correct, response_time_ms, points_earned)
#                 VALUES (%s, %s, %s, %s, %s, %s, %s)
#             """, (game['game_id'], player['player_id'], q['id'], answer, is_correct, int(elapsed_ms), points))
#             cur.close()
#             conn.close()
#         except Exception as e:
#             print(f"Database error: {e}")
        
#         # Publish to Ably
#         _publish_to_ably(f"game:{room_code}", 'answer_received', {
#             'player_token': player_token,
#             'is_correct': is_correct,
#             'points': points,
#             'correct_answer': correct
#         })
        
#         _publish_to_ably(f"host:{room_code}", 'answer_count', {
#             'answered': len(game['current_answers']),
#             'total': len(game['players'])
#         })
        
#         return jsonify({
#             'success': True,
#             'is_correct': is_correct,
#             'points': points,
#             'correct_answer': correct
#         })
#     except Exception as e:
#         print(f"❌ Error: {e}")
#         return jsonify({'error': str(e)}), 500

# # ====================== MAIN ======================
# # Initialize database on startup (for local development)
# init_db()

# # For Vercel serverless
# app.debug = False














# from flask import Flask, render_template, request, jsonify
# from flask_cors import CORS
# import psycopg2
# import psycopg2.extras
# import os
# import json
# import random
# import string
# import qrcode
# import io
# import base64
# import threading
# from datetime import datetime
# from dotenv import load_dotenv
# import hashlib
# import requests
# import traceback
# import time
# import hmac

# load_dotenv()

# app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
# CORS(app)
# app.secret_key = os.environ.get('SECRET_KEY')

# DATABASE_URL = os.environ.get('DATABASE_URL')
# ABLY_API_KEY = os.environ.get('ABLY_API_KEY')

# QUESTION_TIME = 20
# REVEAL_TIME = 6

# active_games = {}

# # ====================== DATABASE ======================
# def get_db():
#     conn = psycopg2.connect(DATABASE_URL)
#     conn.autocommit = True
#     return conn

# # ====================== ABLY FIXED ======================
# def _publish_to_ably(channel, name, data):
#     if not ABLY_API_KEY:
#         print("❌ Missing ABLY_API_KEY")
#         return
#     try:
#         key_name, key_secret = ABLY_API_KEY.split(':')
#         auth = base64.b64encode(f"{key_name}:{key_secret}".encode()).decode()

#         url = f"https://rest.ably.io/channels/{channel}/messages"

#         res = requests.post(
#             url,
#             headers={
#                 'Authorization': f'Basic {auth}',
#                 'Content-Type': 'application/json'
#             },
#             json={'name': name, 'data': data},
#             timeout=5
#         )

#         if res.status_code not in [200, 201]:
#             print(f"❌ Ably publish failed: {res.text}")
#         else:
#             print(f"📡 Sent → {channel} | {name}")

#     except Exception as e:
#         print(f"❌ Ably error {channel} {name}: {e}")

# @app.route('/api/ably-token', methods=['POST'])
# def get_ably_token():
#     try:
#         key_name, key_secret = ABLY_API_KEY.split(':')

#         client_id = request.json.get('player_name', 'guest')

#         ttl = 3600000
#         capability = json.dumps({"*": ["publish", "subscribe"]})
#         timestamp = int(time.time() * 1000)
#         nonce = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

#         sign_text = f"{key_name}\n{ttl}\n{capability}\n{client_id}\n{timestamp}\n{nonce}"

#         mac = hmac.new(
#             key_secret.encode(),
#             sign_text.encode(),
#             hashlib.sha256
#         ).digest()

#         token = {
#             "keyName": key_name,
#             "ttl": ttl,
#             "capability": capability,
#             "clientId": client_id,
#             "timestamp": timestamp,
#             "nonce": nonce,
#             "mac": base64.b64encode(mac).decode()
#         }

#         return jsonify(token)

#     except Exception as e:
#         print(f"❌ Token error: {e}")
#         return jsonify({'error': str(e)}), 500

# # ====================== HELPERS ======================
# def generate_room_code():
#     return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

# def generate_qr_code(url):
#     qr = qrcode.QRCode(version=1, box_size=10, border=4)
#     qr.add_data(url)
#     qr.make(fit=True)
#     img = qr.make_image(fill_color="#1a0a00", back_color="#fff8f0")
#     buf = io.BytesIO()
#     img.save(buf, format='PNG')
#     buf.seek(0)
#     return base64.b64encode(buf.read()).decode()

# # ====================== GAME LOGIC ======================
# def _send_question(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return

#     q_index = game.get('current_question', 0)
#     if q_index >= len(game['question_ids']):
#         _end_game(room_code)
#         return

#     print(f"🔥 Sending question {q_index + 1}")

#     conn = get_db()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
#     cur.execute("SELECT * FROM questions WHERE id = %s", (game['question_ids'][q_index],))
#     q = dict(cur.fetchone())
#     cur.close()
#     conn.close()

#     game['current_question_data'] = q
#     game['current_answers'] = {}
#     game['question_start_time'] = datetime.now().timestamp() * 1000

#     payload = {
#         'question_number': q_index + 1,
#         'total_questions': len(game['question_ids']),
#         'question': q['question'],
#         'options': {
#             'A': q['option_a'],
#             'B': q['option_b'],
#             'C': q['option_c'],
#             'D': q['option_d']
#         },
#         'category': q.get('category', 'General'),
#         'time_limit': QUESTION_TIME
#     }

#     _publish_to_ably(f"game:{room_code}", 'new_question', payload)

#     threading.Timer(QUESTION_TIME, lambda: _auto_reveal(room_code)).start()

# def _auto_reveal(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return

#     print(f"🔥 Revealing answers for room {room_code}")

#     q = game['current_question_data']
    
#     # Count answers
#     counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
#     for answer in game['current_answers'].values():
#         counts[answer['answer']] = counts.get(answer['answer'], 0) + 1

#     players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)

#     payload = {
#         'correct_answer': q['correct_answer'],
#         'answer_counts': counts,
#         'leaderboard': [
#             {'name': p['name'], 'score': p['score']}
#             for p in players_sorted[:10]
#         ],
#         'next_in': REVEAL_TIME
#     }

#     _publish_to_ably(f"game:{room_code}", 'answer_reveal', payload)

#     threading.Timer(REVEAL_TIME, lambda: _advance_question(room_code)).start()

# def _advance_question(room_code):
#     game = active_games.get(room_code)
#     if not game or game['status'] != 'active':
#         return
#     game['current_question'] += 1
#     print(f"🔵 Advancing to question {game['current_question'] + 1}")
#     _send_question(room_code)

# def _end_game(room_code):
#     game = active_games.get(room_code)
#     if not game:
#         return

#     game['status'] = 'ended'
#     print(f"✅ Game ended in room {room_code}")

#     players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)
    
#     payload = {
#         'top3': [{'name': p['name'], 'score': p['score']} for p in players_sorted[:3]],
#         'all_players': [{'name': p['name'], 'score': p['score']} for p in players_sorted],
#     }

#     _publish_to_ably(f"game:{room_code}", 'game_over', payload)

# # ====================== ROUTES ======================
# @app.route('/')
# def index():
#     return render_template('index.html')

# @app.route('/host')
# def host():
#     return render_template('host.html')

# @app.route('/play')
# def play():
#     return render_template('play.html')

# @app.route('/join/<room_code>')
# def join_via_link(room_code):
#     return render_template('play.html', room_code=room_code)

# @app.route('/api/create-game', methods=['POST'])
# def create_game():
#     conn = get_db()
#     cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

#     cur.execute("SELECT id FROM questions ORDER BY RANDOM() LIMIT 10")
#     ids = [r['id'] for r in cur.fetchall()]

#     room_code = generate_room_code()

#     cur.execute("INSERT INTO games (room_code, question_ids) VALUES (%s, %s) RETURNING id",
#                 (room_code, json.dumps(ids)))
#     game_id = cur.fetchone()['id']

#     cur.close()
#     conn.close()

#     active_games[room_code] = {
#         'game_id': game_id,
#         'room_code': room_code,
#         'status': 'waiting',
#         'current_question': 0,
#         'question_ids': ids,
#         'players': {},
#         'current_answers': {},
#         'current_question_data': None,
#         'question_start_time': None
#     }

#     join_url = f"{request.host_url}join/{room_code}"
#     qr = generate_qr_code(join_url)

#     return jsonify({
#         'room_code': room_code,
#         'join_url': join_url,
#         'qr_code': qr
#     })

# @app.route('/api/join-game', methods=['POST'])
# def join_game():
#     data = request.json
#     room_code = data.get('room_code')
#     name = data.get('name')

#     game = active_games.get(room_code)
#     if not game:
#         return jsonify({'error': 'Invalid room'}), 400

#     # Check for duplicate names
#     for p in game['players'].values():
#         if p['name'].lower() == name.lower():
#             return jsonify({'error': 'Name already taken!'}), 400

#     token = hashlib.md5(f"{name}{datetime.now()}".encode()).hexdigest()[:12]

#     # Save to database
#     conn = get_db()
#     cur = conn.cursor()
#     cur.execute("""
#         INSERT INTO players (game_id, name, player_token, score) 
#         VALUES (%s, %s, %s, %s) RETURNING id
#     """, (game['game_id'], name, token, 0))
#     player_id = cur.fetchone()[0]
#     cur.close()
#     conn.close()

#     game['players'][token] = {
#         'player_id': player_id,
#         'name': name,
#         'score': 0,
#         'token': token
#     }

#     _publish_to_ably(f"host:{room_code}", 'player_list_update', {
#         'players': [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
#     })

#     return jsonify({'player_token': token})

# @app.route('/api/game/<room_code>/players', methods=['GET'])
# def get_game_players(room_code):
#     if room_code not in active_games:
#         return jsonify({'players': []})
    
#     game = active_games[room_code]
#     players = [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
#     return jsonify({'players': players})

# @app.route('/api/start-game', methods=['POST'])
# def start_game():
#     room_code = request.json.get('room_code')
#     game = active_games.get(room_code)

#     if not game:
#         return jsonify({'error': 'Game not found'}), 404
    
#     if len(game['players']) < 1:
#         return jsonify({'error': 'Need at least 1 player!'}), 400

#     if game['status'] != 'waiting':
#         return jsonify({'error': 'Game already started!'}), 400

#     game['status'] = 'active'

#     _publish_to_ably(f"game:{room_code}", 'game_started', {})
#     threading.Timer(1, lambda: _send_question(room_code)).start()

#     return jsonify({'success': True})

# @app.route('/api/submit-answer', methods=['POST'])
# def submit_answer():
#     try:
#         data = request.json
#         print(f"🔵 SUBMIT ANSWER RAW DATA: {data}")
        
#         room_code = data.get('room_code')
#         answer = data.get('answer', '').upper()
#         player_token = data.get('player_token')
        
#         print(f"🔵 Room: {room_code}, Answer: {answer}, Player Token: {player_token}")
        
#         if room_code not in active_games:
#             print(f"❌ Game not found: {room_code}")
#             return jsonify({'error': 'Game not found!'}), 404
        
#         game = active_games[room_code]
#         print(f"🔵 Game status: {game['status']}")
        
#         if game['status'] != 'active':
#             print(f"❌ Game not active")
#             return jsonify({'error': 'Game not active!'}), 400
        
#         if player_token not in game['players']:
#             print(f"❌ Player not found. Available players: {list(game['players'].keys())}")
#             return jsonify({'error': 'Player not found!'}), 404
        
#         if player_token in game['current_answers']:
#             print(f"❌ Already answered")
#             return jsonify({'error': 'Already answered!'}), 400
        
#         if game['question_start_time'] is None:
#             print(f"❌ No active question")
#             return jsonify({'error': 'No active question!'}), 400
        
#         player = game['players'][player_token]
#         q = game['current_question_data']
        
#         if not q:
#             print(f"❌ No question data")
#             return jsonify({'error': 'No active question!'}), 400
        
#         elapsed_ms = (datetime.now().timestamp() * 1000) - game['question_start_time']
        
#         if elapsed_ms > (QUESTION_TIME * 1000):
#             print(f"❌ Time expired: {elapsed_ms}ms > {QUESTION_TIME * 1000}ms")
#             return jsonify({'error': 'Time expired!'}), 400
        
#         correct = q['correct_answer'].upper()
#         is_correct = answer == correct
        
#         if is_correct:
#             time_factor = max(0, 1 - (elapsed_ms / (QUESTION_TIME * 1000)))
#             points = int(500 + 500 * time_factor)
#         else:
#             points = 0
        
#         print(f"✅ Answer processed: correct={is_correct}, points={points}")
        
#         game['current_answers'][player_token] = {
#             'answer': answer,
#             'is_correct': is_correct,
#             'points': points
#         }
#         player['score'] += points
        
#         try:
#             conn = get_db()
#             cur = conn.cursor()
#             cur.execute("""
#                 INSERT INTO answers (game_id, player_id, question_id, answer, is_correct, response_time_ms, points_earned)
#                 VALUES (%s, %s, %s, %s, %s, %s, %s)
#             """, (game['game_id'], player['player_id'], q['id'], answer, is_correct, int(elapsed_ms), points))
#             cur.close()
#             conn.close()
#             print(f"✅ Answer saved to database")
#         except Exception as e:
#             print(f"❌ Database error saving answer: {e}")
        
#         # Publish to Ably
#         if ABLY_API_KEY:
#             try:
#                 # Send answer received to game channel
#                 _publish_to_ably(f"game:{room_code}", 'answer_received', {
#                     'player_token': player_token,
#                     'is_correct': is_correct,
#                     'points': points,
#                     'correct_answer': correct
#                 })
                
#                 # Update answer count for host
#                 _publish_to_ably(f"host:{room_code}", 'answer_count', {
#                     'answered': len(game['current_answers']),
#                     'total': len(game['players'])
#                 })
#             except Exception as e:
#                 print(f"❌ Ably publish error: {e}")
        
#         return jsonify({
#             'success': True,
#             'is_correct': is_correct,
#             'points': points,
#             'correct_answer': correct
#         })
#     except Exception as e:
#         print(f"❌ Error submitting answer: {e}")
#         traceback.print_exc()
#         return jsonify({'error': str(e)}), 500

# # ====================== MAIN ======================
# if __name__ == '__main__':
#     port = int(os.environ.get('PORT', 5001))
#     print(f"🚀 Running on http://localhost:{port}")
#     app.run(debug=True, host='0.0.0.0', port=port)











