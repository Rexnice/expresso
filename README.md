# ☕ Expresso — Quiz Game

A beautiful, real-time Kahoot-style quiz game built with Flask, WebSockets, and Neon PostgreSQL.

---

## 🚀 Quick Start

### 1. Set up Neon PostgreSQL

1. Go to [neon.tech](https://neon.tech) and create a free account
2. Create a new project called `expresso`
3. Copy the connection string (it looks like `postgresql://user:pass@host.neon.tech/dbname?sslmode=require`)

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env and paste your Neon DATABASE_URL
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the App

```bash
python app.py
```

Open **http://localhost:5000** in your browser.

The database tables and all 26 questions will be created automatically on first run.

---

## 🎮 How to Play

### As Host:
1. Go to `/host`
2. Select questions (or click "Random 15")
3. Click **"Brew Game"** — you'll get a **room code** and **QR code**
4. Players scan the QR or visit the join URL
5. Once players are in the lobby, click **"Start Game"**
6. After each question, click **"Reveal Answer"** to show results + leaderboard
7. Click **"Next Question"** to continue
8. At the end, the **Top 3 podium** is displayed 🏆

### As Player:
1. Scan the QR code OR go to `/play` and enter the room code
2. Choose your name
3. Wait in the lobby until the host starts
4. Tap an answer as fast as possible (faster = more points, max 1000 per question)
5. See if you're correct after reveal
6. Check the final leaderboard!

---

## ⚙️ Admin Panel

Go to `/admin` to:
- Add new questions
- Edit existing questions  
- Delete questions
- Filter by category (Bible, Culture, Poland, General…)
- Set the correct answer with colour-coded buttons

---

## 📁 Project Structure

```
expresso/
├── app.py              # Flask app + Socket.IO + API routes
├── requirements.txt    # Python dependencies
├── .env.example        # Environment template
├── README.md
└── templates/
    ├── index.html      # Landing page
    ├── host.html       # Host dashboard (setup + lobby + game control)
    ├── play.html       # Player view (join + game)
    └── admin.html      # Question management
```

---

## 🏆 Scoring System

- Correct answer: **500–1000 points** (based on speed)
- Wrong answer: **0 points**
- Faster answers = more points
- Maximum 1000 pts per question (answer instantly)
- Minimum 500 pts for correct (answer at last second)

---

## 🛠 Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python Flask + Flask-SocketIO |
| Real-time | WebSockets (Socket.IO) |
| Database | Neon PostgreSQL (serverless) |
| QR Codes | qrcode + Pillow |
| Frontend | Vanilla HTML/CSS/JS |
| Fonts | Playfair Display + DM Sans |

---

## 🌐 Deployment

To deploy on a server, set the environment variables and run:

```bash
gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
         -w 1 -b 0.0.0.0:5000 app:app
```

Or use a platform like Railway, Render, or Fly.io with the provided requirements.
