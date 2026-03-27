from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room
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

load_dotenv()

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
app.secret_key = os.environ.get('SECRET_KEY', 'expresso-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

DATABASE_URL = os.environ.get('DATABASE_URL', 'postgresql://localhost/expresso')

QUESTION_TIME = 20   # seconds players have to answer
REVEAL_TIME   = 6    # seconds to show leaderboard before next question

def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn

def init_db():
    conn = get_db()
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
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            game_id INT REFERENCES games(id),
            name VARCHAR(50) NOT NULL,
            socket_id VARCHAR(100),
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

def insert_default_questions(cur):
    questions = [
        ("Which prophet was thrown into a den of lions?", "Jeremiah", "Daniel", "Elijah", "Elisha", "B", "Bible"),
        ("What is the traditional Yoruba male attire called?", "Kente", "Smock", "Agbada", "Dashiki", "C", "Culture"),
        ("In Ghana, which soup pairs with Banku and Fufu?", "Pepper soup", "Groundnut soup", "Ogbono soup", "Palm nut soup", "D", "Culture"),
        ("Which disciple asked to walk on water with Jesus?", "John", "James", "Andrew", "Peter", "D", "Bible"),
        ("What Nigerian snack is made from beans and fried in oil?", "Puff-puff", "Moi moi", "Akara", "Chin Chin", "C", "Culture"),
        ("Where did Jesus perform His first miracle (water to wine)?", "Nazareth", "Jerusalem", "Cana", "Bethlehem", "C", "Bible"),
        ("The traditional Kente cloth originates from which ethnic group?", "Yoruba", "Hausa", "Igbo", "Ashanti", "D", "Culture"),
        ("What did God provide for the Israelites in the wilderness?", "Quail", "Water", "Manna", "Figs", "C", "Bible"),
        ("Which Nigerian dish is made from pounded cassava?", "Amala", "Eba", "Fufu", "Semo", "B", "Culture"),
        ("Who was Paul's companion on his first missionary journey?", "Silas", "Timothy", "Barnabas", "Luke", "C", "Bible"),
        ("The 'Smock' (Fugu) is traditional attire from which region?", "South-South Nigeria", "Northern Ghana", "South-East Nigeria", "Coastal Kenya", "B", "Culture"),
        ("Which king tried to arrest Elisha but was struck with blindness?", "Ahab", "Jehu", "Jehoram", "Ben-Hadad", "D", "Bible"),
        ("Waakye is a Ghanaian dish made from rice and which legume?", "Black-eyed peas", "Cowpeas", "Beans", "Millet", "A", "Culture"),
        ("Which book contains the Proverbs of Solomon?", "Psalms", "Ecclesiastes", "Proverbs", "Job", "C", "Bible"),
        ("The 'Gele' is a vital accessory in Nigerian culture. What is it?", "Necklace", "Head-tie", "Wrapper", "Beads", "B", "Culture"),
        ("Katowice is located in which historical region of Poland?", "Mazovia", "Pomerania", "Silesia", "Lesser Poland", "C", "Poland"),
        ("The famous Spodek arena in Katowice resembles what?", "A flying saucer", "A saucer (spodek)", "A coal mine", "A church spire", "B", "Poland"),
        ("Which river flows through Katowice?", "Vistula", "Oder", "Rawa", "Warta", "C", "Poland"),
        ("Katowice is the heart of which industrial region?", "Upper Silesia", "Lower Silesia", "Zagłębie", "Greater Poland", "A", "Poland"),
        ("The 'Superjednostka' in Katowice is known for being what?", "A cathedral", "A long residential block", "A coal mine shaft", "A university library", "B", "Poland"),
        ("Nikiszowiec district was originally built for whom?", "Steelworkers", "Coal miners", "Railway workers", "Brewers", "B", "Poland"),
        ("Which city is Katowice's closest neighbor in Upper Silesia?", "Kraków", "Wrocław", "Chorzów", "Częstochowa", "C", "Poland"),
        ("The Silesian Museum in Katowice is built on a former what?", "Coal mine", "Brewery", "Prison", "Steel factory", "A", "Poland"),
        ("The name 'Katowice' derives from which Polish word?", "Kot (cat)", "Kąt (corner)", "Kopa (heap)", "Kat (executioner)", "A", "Poland"),
        ("Which event has been hosted at Spodek since 2014?", "Eurovision", "MTV Europe Music Awards", "Chopin Competition", "Woodstock Festival", "B", "Poland"),
        ("Ulica Mariacka in Katowice is known for its abundance of what?", "Churches", "Parks", "Restaurants and pubs", "Museums", "C", "Poland"),
    ]
    cur.executemany("""
        INSERT INTO questions (question, option_a, option_b, option_c, option_d, correct_answer, category)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, questions)

# ── In-memory state ───────────────────────────────────────
active_games = {}
game_timers  = {}  # room_code -> threading.Timer

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

def cancel_timer(room_code):
    t = game_timers.pop(room_code, None)
    if t:
        t.cancel()

def schedule(room_code, delay, fn):
    cancel_timer(room_code)
    t = threading.Timer(delay, fn)
    t.daemon = True
    t.start()
    game_timers[room_code] = t

# ── Routes ────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/host')
def host():
    return render_template('host.html')

@app.route('/admin')
def admin():
    return render_template('admin.html')

@app.route('/play')
def play():
    return render_template('play.html')

@app.route('/join/<room_code>')
def join_via_link(room_code):
    return render_template('play.html', room_code=room_code)

# ── API ───────────────────────────────────────────────────
@app.route('/api/questions', methods=['GET'])
def get_questions():
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM questions ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify(rows)

@app.route('/api/questions', methods=['POST'])
def add_question():
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO questions (question,option_a,option_b,option_c,option_d,correct_answer,category)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (d['question'], d['option_a'], d['option_b'], d['option_c'], d['option_d'],
          d['correct_answer'].upper(), d.get('category', 'General')))
    new_id = cur.fetchone()[0]
    cur.close(); conn.close()
    return jsonify({'id': new_id})

@app.route('/api/questions/<int:qid>', methods=['PUT'])
def update_question(qid):
    d = request.json
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        UPDATE questions SET question=%s,option_a=%s,option_b=%s,
        option_c=%s,option_d=%s,correct_answer=%s,category=%s WHERE id=%s
    """, (d['question'], d['option_a'], d['option_b'], d['option_c'], d['option_d'],
          d['correct_answer'].upper(), d.get('category', 'General'), qid))
    cur.close(); conn.close()
    return jsonify({'message': 'Updated'})

@app.route('/api/questions/<int:qid>', methods=['DELETE'])
def delete_question(qid):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM questions WHERE id=%s", (qid,))
    cur.close(); conn.close()
    return jsonify({'message': 'Deleted'})

@app.route('/api/create-game', methods=['POST'])
def create_game():
    data = request.json or {}
    ids  = data.get('question_ids', [])
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    if not ids:
        cur.execute("SELECT id FROM questions ORDER BY RANDOM() LIMIT 15")
        ids = [r['id'] for r in cur.fetchall()]
    room_code = generate_room_code()
    while True:
        cur.execute("SELECT id FROM games WHERE room_code=%s", (room_code,))
        if not cur.fetchone(): break
        room_code = generate_room_code()
    cur.execute("INSERT INTO games (room_code,question_ids) VALUES (%s,%s) RETURNING id",
                (room_code, json.dumps(ids)))
    game_id = cur.fetchone()['id']
    cur.close(); conn.close()

    active_games[room_code] = {
        'game_id': game_id, 'room_code': room_code,
        'status': 'waiting', 'current_question': 0,
        'question_ids': ids, 'players': {},
        'question_start_time': None, 'current_answers': {},
        'current_question_data': None,
    }

    public_url = os.environ.get('PUBLIC_URL', '').rstrip('/')
    if not public_url:
        public_url = request.host_url.rstrip('/')
    join_url = f"{public_url}/join/{room_code}"
    qr_b64   = generate_qr_code(join_url)

    return jsonify({
        'room_code': room_code, 'game_id': game_id,
        'join_url': join_url, 'qr_code': qr_b64,
        'total_questions': len(ids)
    })

# ── Socket Events ─────────────────────────────────────────
@socketio.on('host_join')
def handle_host_join(data):
    room_code = data['room_code']
    join_room(room_code)
    join_room(f"host_{room_code}")
    emit('host_joined', {'room_code': room_code})

@socketio.on('player_join')
def handle_player_join(data):
    room_code = data['room_code'].upper().strip()
    name      = data['name'].strip()[:20]
    if room_code not in active_games:
        emit('error', {'message': 'Game not found! Check the code.'}); return
    game = active_games[room_code]
    if game['status'] != 'waiting':
        emit('error', {'message': 'Game already started!'}); return
    if not name:
        emit('error', {'message': 'Please enter a name!'}); return
    for p in game['players'].values():
        if p['name'].lower() == name.lower():
            emit('error', {'message': 'Name already taken!'}); return

    sid = request.sid
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO players (game_id,name,socket_id) VALUES (%s,%s,%s) RETURNING id",
                (game['game_id'], name, sid))
    player_id = cur.fetchone()[0]
    cur.close(); conn.close()

    game['players'][sid] = {'player_id': player_id, 'name': name, 'score': 0, 'sid': sid}
    join_room(room_code)
    emit('joined_game', {'name': name, 'room_code': room_code})
    _broadcast_players(room_code)

@socketio.on('start_game')
def handle_start_game(data):
    room_code = data['room_code']
    if room_code not in active_games: return
    game = active_games[room_code]
    if len(game['players']) < 1:
        emit('error', {'message': 'Need at least 1 player!'}); return
    game['status'] = 'active'
    socketio.emit('game_started', {}, room=room_code)
    _send_question(room_code)

@socketio.on('submit_answer')
def handle_answer(data):
    room_code  = data['room_code']
    answer     = data['answer'].upper()
    sid        = request.sid
    if room_code not in active_games: return
    game = active_games[room_code]
    if sid not in game['players']: return
    if sid in game['current_answers']: return  # already answered

    player     = game['players'][sid]
    q          = game['current_question_data']
    correct    = q['correct_answer'].upper()
    is_correct = answer == correct
    elapsed_ms = (datetime.now().timestamp() * 1000) - game['question_start_time']

    if is_correct:
        time_factor = max(0, 1 - (elapsed_ms / (QUESTION_TIME * 1000)))
        points = int(500 + 500 * time_factor)
    else:
        points = 0

    game['current_answers'][sid] = {'answer': answer, 'is_correct': is_correct, 'points': points}
    player['score'] += points

    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO answers (game_id,player_id,question_id,answer,is_correct,response_time_ms,points_earned)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (game['game_id'], player['player_id'], q['id'],
          answer, is_correct, int(elapsed_ms), points))
    cur.close(); conn.close()

    # Only this player gets their result — timer keeps running for everyone
    emit('answer_received', {'is_correct': is_correct, 'points': points, 'correct_answer': correct})

    # Update host count display
    socketio.emit('answer_count', {
        'answered': len(game['current_answers']),
        'total':    len(game['players'])
    }, room=f"host_{room_code}")

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for room_code, game in list(active_games.items()):
        if sid in game['players']:
            del game['players'][sid]
            _broadcast_players(room_code)
            break

# ── Internal helpers ──────────────────────────────────────
def _broadcast_players(room_code):
    game = active_games.get(room_code)
    if not game: return
    players = [{'name': p['name'], 'score': p['score']} for p in game['players'].values()]
    socketio.emit('player_list_update', {'players': players}, room=f"host_{room_code}")

def _send_question(room_code):
    game = active_games.get(room_code)
    if not game or game['status'] != 'active': return

    q_index = game['current_question']
    if q_index >= len(game['question_ids']):
        _end_game(room_code); return

    q_id = game['question_ids'][q_index]
    conn = get_db()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM questions WHERE id=%s", (q_id,))
    q = dict(cur.fetchone())
    cur.close(); conn.close()

    game['current_question_data'] = q
    game['question_start_time']   = datetime.now().timestamp() * 1000
    game['current_answers']       = {}

    payload = {
        'question_number': q_index + 1,
        'total_questions': len(game['question_ids']),
        'question':        q['question'],
        'options': {'A': q['option_a'], 'B': q['option_b'],
                    'C': q['option_c'], 'D': q['option_d']},
        'category':        q.get('category', 'General'),
        'time_limit':      QUESTION_TIME,
    }
    socketio.emit('new_question', payload, room=room_code)

    # Server owns the timer — auto reveal when time is up
    schedule(room_code, QUESTION_TIME, lambda: _auto_reveal(room_code))

def _auto_reveal(room_code):
    game = active_games.get(room_code)
    if not game or game['status'] != 'active': return

    q      = game['current_question_data']
    counts = {'A': 0, 'B': 0, 'C': 0, 'D': 0}
    for a in game['current_answers'].values():
        counts[a['answer']] = counts.get(a['answer'], 0) + 1

    players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)

    socketio.emit('answer_reveal', {
        'correct_answer': q['correct_answer'].upper(),
        'answer_counts':  counts,
        'leaderboard':    [{'name': p['name'], 'score': p['score']} for p in players_sorted[:10]],
        'options': {'A': q['option_a'], 'B': q['option_b'],
                    'C': q['option_c'], 'D': q['option_d']},
        'next_in':        REVEAL_TIME,
    }, room=room_code)

    # Auto-advance after REVEAL_TIME
    schedule(room_code, REVEAL_TIME, lambda: _advance_question(room_code))

def _advance_question(room_code):
    game = active_games.get(room_code)
    if not game or game['status'] != 'active': return
    game['current_question'] += 1
    if game['current_question'] >= len(game['question_ids']):
        _end_game(room_code)
    else:
        _send_question(room_code)

def _end_game(room_code):
    game = active_games.get(room_code)
    if not game: return
    game['status'] = 'ended'
    cancel_timer(room_code)

    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE games SET status='ended' WHERE room_code=%s", (room_code,))
    cur.close(); conn.close()

    players_sorted = sorted(game['players'].values(), key=lambda x: x['score'], reverse=True)
    socketio.emit('game_over', {
        'top3':        [{'name': p['name'], 'score': p['score']} for p in players_sorted[:3]],
        'all_players': [{'name': p['name'], 'score': p['score']} for p in players_sorted],
    }, room=room_code)

if __name__ == '__main__':
    init_db()

    # ── Try ngrok for public internet access ──────────────
    port = int(os.environ.get('PORT', 5001))
    try:
        from pyngrok import ngrok
        tunnel = ngrok.connect(port, bind_tls=True)
        public_url = tunnel.public_url
        os.environ['PUBLIC_URL'] = public_url
        print(f"\n{'='*55}")
        print(f"  ☕  EXPRESSO — Live on the internet!")
        print(f"  🌍  Public URL : {public_url}")
        print(f"  🎮  Host game  : {public_url}/host")
        print(f"  📱  Players    : {public_url}/play")
        print(f"{'='*55}\n")
    except Exception as e:
        print(f"\n⚠️  ngrok not found — local only (http://127.0.0.1:{port})")
        print(f"   Run: pip install pyngrok  to enable public access\n")

    socketio.run(app, debug=False, host='0.0.0.0', port=port)