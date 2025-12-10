import os
import requests
import oracledb
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime

# 모듈 import
from config import UPLOAD_FOLDER, SPOTIFY_API_BASE
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data
from utils import allowed_file, verify_turnstile, get_spotify_headers, get_current_weather, get_today_holiday, extract_spotify_id

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CORS(app)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db_pool()

# =========================================================
# [매핑 데이터]
# =========================================================
HOLIDAY_MAPPING = {
    "신정": {"tag": "tag:Rest", "date_type": "2"},
    "설날": {"tag": "tag:Family", "date_type": "2"},
    "삼일절": {"tag": "tag:Memorial", "date_type": "2"},
    "어린이날": {"tag": "tag:Exciting", "date_type": "2"},
    "광복절": {"tag": "tag:Memorial", "date_type": "2"},
    "추석": {"tag": "tag:Family", "date_type": "2"},
    "개천절": {"tag": "tag:Memorial", "date_type": "2"},
    "한글날": {"tag": "tag:Korea", "date_type": "2"},
    "크리스마스": {"tag": "tag:Christmas", "date_type": "2"},
    "석가탄신일": {"tag": "tag:Rest", "date_type": "2"}
}

WEATHER_MAPPING = {
    "Rain": {"label": "비", "tag": "tag:Rain", "code": "1"},
    "Snow": {"label": "눈", "tag": "tag:Snow", "code": "3"},
    "Clear": {"label": "맑음", "tag": "tag:Clear", "code": "0"}
}

# =========================================================
# 3. 데이터 제공 API
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def admin_update_movies():
    try:
        msg = update_box_office_data()
        return jsonify({"message": msg}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .",
            "",
            "# Box Office Movies & OST",
            "",
        ]

        seen_movies = set()
        for r in rows:
            mid, title, rank, poster, tid, ttitle, artist, tcover, audio = r
            if title in seen_movies: continue # 중복 방지
            seen_movies.add(title)
            
            movie_uri = f"<https://knowledgemap.kr/resource/movie/{mid}>"
            ttl_parts.append(f"""
{movie_uri} a schema:Movie ;
    schema:name "{title}" ;
    schema:position "{rank}" ;
    schema:image "{poster or ''}" .""")

            if tid:
                ttl_parts.append(f"""
<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ;
    schema:name "{ttitle}" ;
    schema:byArtist "{artist}" ;
    schema:image "{tcover}" ;
    schema:audio "{audio or ''}" ;
    komc:featuredIn {movie_uri} ;
    komc:relatedTag tag:MovieOST .""")

        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        return str(e), 500

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    try:
        weather_code = get_current_weather()
        holiday_name = get_today_holiday()
        hour = datetime.now().hour

        detected_triples = []
        target_tag = "tag:Pop"
        context_uri = ""
        pref_label = ""
        definition = ""

        if holiday_name:
            info = HOLIDAY_MAPPING.get(holiday_name, {"tag": "tag:Rest", "date_type": "2"})
            target_tag = info["tag"]
            context_uri = f"http://knowledgemap.kr/komc/holiday/{holiday_name}"
            pref_label = f"특별한 날 ({holiday_name})"
            definition = f"오늘은 {holiday_name}! 즐거운 하루 보내세요 🎉"
            detected_triples.append(f"<{context_uri}> a komc:HolidayContext ; schema:name \"{holiday_name}\" ; komc:relatedTag {target_tag} .")

        elif weather_code in ["Rain", "Snow"]:
            info = WEATHER_MAPPING[weather_code]
            target_tag = info["tag"]
            context_uri = f"https://knowledgemap.kr/komc/weather/{weather_code}"
            pref_label = f"{info['label']} 오는 날"
            definition = f"창밖의 {info['label']}와 어울리는 감성 ☔"
            detected_triples.append(f"<{context_uri}> a schema:WeatherForecast ; schema:weatherCondition \"{info['label']}\" ; komc:relatedTag {target_tag} .")

        else:
            time_slot = "Night" if (22 <= hour or hour < 6) else "Day"
            if 6 <= hour < 12: time_slot = "Morning"
            elif 18 <= hour < 22: time_slot = "Evening"

            context_uri = f"https://knowledgemap.kr/komc/time/{time_slot}"
            tag_map = {"Morning": "tag:Clear", "Day": "tag:Exciting", "Evening": "tag:Sentimental", "Night": "tag:Rest"}
            target_tag = tag_map.get(time_slot, "tag:Pop")
            pref_label = time_slot
            definition = "음악과 함께하는 시간"
            detected_triples.append(f"<{context_uri}> a komc:TimeContext ; skos:prefLabel \"{time_slot}\"@ko ; komc:relatedTag {target_tag} .")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM TRACKS t
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            WHERE tt.tag_id = :tag_uri
            FETCH FIRST 6 ROWS ONLY
        """, {"tag_uri": target_tag})
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "",
            "\n".join(detected_triples),
            f"""
komc:CurrentContext a skos:Concept ;
    skos:prefLabel "{pref_label}"@ko ;
    skos:definition "{definition}"@ko ;
    komc:derivedFrom <{context_uri}> ;
    komc:relatedTag {target_tag} .
"""
        ]
        
        track_data = []
        for r in rows:
            tid, title, artist, cover, preview = r
            track_uri = f"<https://knowledgemap.kr/resource/track/{tid}>"
            ttl_parts.append(f"""
{track_uri} a schema:MusicRecording ;
    schema:name "{title}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    schema:audio "{preview}" .""")
            track_data.append({"title": title, "artist": artist, "cover": cover, "preview": preview})

        return jsonify({
            "ttl": "\n".join(ttl_parts),
            "message": definition,
            "tracks": track_data,
            "tags": [target_tag.replace("tag:", "")]
        })

    except Exception as e:
        print(f"[Context Error] {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def proxy_search():
    try:
        q = request.args.get('q'); offset = request.args.get('offset', '0')
        if not q: return jsonify({"error": "No query"}), 400
        headers = get_spotify_headers()
        params = {"q": q, "type": "track,album,artist", "limit": "20", "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        return jsonify(res.json()), res.status_code
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<track_id>.ttl', methods=['GET'])
def get_track_detail_ttl(track_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.track_title, t.artist_name, t.album_id, t.preview_url, t.image_url, 
                   t.bpm, t.music_key, t.duration, t.views
            FROM TRACKS t
            WHERE t.track_id = :1
        """, [track_id])
        track_row = cursor.fetchone()

        if not track_row: return "Track not found", 404

        title, artist, album_id, preview, cover, bpm, key, duration, views = track_row
        
        cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :1", [track_id])
        tags = [row[0] for row in cursor.fetchall()]
        tag_str = ", ".join(tags) if tags else "tag:Music"

        ttl_content = f"""@prefix schema: <http://schema.org/> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .

<https://knowledgemap.kr/resource/track/{track_id}>
    a schema:MusicRecording ;
    schema:name "{title}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    komc:relatedTag {tag_str} .
"""
        return make_response(ttl_content, 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        return str(e), 500

# --- 인증 및 기타 API (로그인, 회원가입 등) ---
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json
    uid = d.get('id'); pw = d.get('password'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except: return jsonify({"error": "Fail"}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json
    uid = d.get('id'); pw = d.get('password')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): 
            return jsonify({"message": "Success", "user": {"id": u[0], "nickname": u[2], "profile_img": u[3], "role": u[4]}})
        return jsonify({"error": "Invalid"}), 401
    except: return jsonify({"error": "Error"}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)