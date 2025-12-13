import os
import requests
import oracledb
import base64
import re
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta

from config import UPLOAD_FOLDER, SPOTIFY_API_BASE
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data, save_track_details
from utils import allowed_file, verify_turnstile, get_spotify_headers, get_current_weather, get_today_holiday, extract_spotify_id

try:
    from skos_manager import SkosManager
    skos_manager = SkosManager("skos-definition.ttl")
except:
    skos_manager = None

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CORS(app)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db_pool()

# ... (관리자 로그 API 등 기존 코드는 그대로 유지) ...
@app.route('/api/admin/logs', methods=['GET'])
def get_admin_logs():
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT l.log_id, l.target_type, l.target_id, l.action_type, 
                   l.previous_value, l.new_value, l.created_at, u.nickname
            FROM MODIFICATION_LOGS l
            LEFT JOIN USERS u ON l.user_id = u.user_id
            ORDER BY l.created_at DESC
            FETCH FIRST 50 ROWS ONLY
        """)
        rows = cur.fetchall()
        logs = [{"id":r[0], "type":r[1], "target":r[2], "action":r[3], "prev":r[4], "new":r[5], "date":r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else "", "user":r[7] or "Unknown"} for r in rows]
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def admin_update_movies():
    try: return jsonify({"message": update_box_office_data()})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    try:
        weather = get_current_weather()
        holiday = get_today_holiday()
        return jsonify({"message": f"현재 날씨: {weather}", "weather": weather, "holiday": holiday, "tracks": []})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            ORDER BY m.rank ASC
        """)
        rows = cur.fetchall()
        ttl_parts = ["@prefix schema: <http://schema.org/> .", "@prefix komc: <https://knowledgemap.kr/komc/def/> .", ""]
        seen = set()
        for r in rows:
            mid_raw, title, rank, poster = r[0], r[1], r[2], r[3]
            if not mid_raw or title in seen: continue
            seen.add(title)
            mid = base64.urlsafe_b64encode(str(mid_raw).encode()).decode().rstrip("=")
            img = poster or "img/playlist-placeholder.png"
            ttl_parts.append(f"""<https://knowledgemap.kr/resource/movie/{mid}> a schema:Movie ; schema:name "{title}" ; komc:rank {rank} ; schema:image "{img}" .""")
            if r[4]:
                tid = r[4]
                ttl_parts.append(f"""<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ; schema:name "{r[5]}" ; schema:byArtist "{r[6]}" ; schema:image "{r[7] or img}" ; komc:featuredIn <https://knowledgemap.kr/resource/movie/{mid}> .""")
        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})
    except Exception as e: return make_response(f"# Error: {str(e)}", 500, {'Content-Type': 'text/turtle'})

# =========================================================
# 3. 검색 API (최종 수정: 하이브리드 검색)
# =========================================================
@app.route('/api/search', methods=['GET'])
def api_search():
    q = request.args.get('q', '')
    offset = int(request.args.get('offset', '0'))
    
    if not q: return jsonify({"error": "No query"}), 400

    db_items = []
    
    # 1. 태그 검색인 경우 (DB 우선 조회)
    if q.startswith('tag:'):
        try:
            print(f"🔎 [Search] DB 태그 검색 시도: {q}")
            conn = get_db_connection()
            cur = conn.cursor()
            
            # [핵심] LOWER()를 사용하여 대소문자 무시 검색 (jpop == JPop)
            cur.execute("""
                SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url, a.album_title
                FROM TRACKS t 
                JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
                LEFT JOIN ALBUMS a ON t.album_id = a.album_id
                WHERE LOWER(tt.tag_id) = LOWER(:tag)
                ORDER BY t.views DESC
            """, [q.strip()]) 
            
            rows = cur.fetchall()
            
            for r in rows:
                db_items.append({
                    "id": r[0],
                    "name": f"[추천] {r[1]}", # 제목 앞에 [추천] 태그를 붙여서 눈에 띄게 함
                    "artists": [{"name": r[2]}],
                    "album": {
                        "name": r[5] or "Unknown",
                        "images": [{"url": r[3] or "img/playlist-placeholder.png"}]
                    },
                    "preview_url": r[4],
                    "external_urls": {"spotify": f"http://googleusercontent.com/spotify.com/{r[0]}"},
                    "is_local": True # 로컬 데이터임을 표시
                })
            print(f"✅ DB 검색 결과: {len(db_items)}건")
            
        except Exception as e:
            print(f"❌ DB 검색 오류: {e}")

    # 2. Spotify 검색 (DB 결과가 적을 때 보충하거나, 항상 검색)
    spotify_items = []
    try:
        headers = get_spotify_headers()
        params = {"q": q, "type": "track", "limit": "20", "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        if res.status_code == 200:
            spotify_items = res.json().get('tracks', {}).get('items', [])
    except Exception as e:
        print(f"❌ Spotify 검색 오류: {e}")

    # 3. 결과 합치기 (DB 결과가 무조건 위로 오도록)
    # 중복 제거 (Spotify에도 같은 곡이 있을 수 있으므로)
    seen_ids = set()
    final_items = []
    
    # DB 결과 먼저 넣기
    for item in db_items:
        if item['id'] not in seen_ids:
            final_items.append(item)
            seen_ids.add(item['id'])
            
    # Spotify 결과 뒤에 붙이기
    for item in spotify_items:
        if item['id'] not in seen_ids:
            final_items.append(item)
            seen_ids.add(item['id'])

    return jsonify({
        "tracks": {
            "items": final_items,
            "total": len(final_items),
            "offset": offset
        }
    })

# ... (나머지 유저, 파일 관련 API 코드는 기존과 동일하게 유지) ...
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('id'); pw = d.get('password'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except: return jsonify({"error": "Fail"}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('id'); pw = d.get('password')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"message":"OK", "user": {"id":u[0], "nickname":u[2], "profile_img":u[3], "role":u[4]}})
        return jsonify({"error": "Invalid"}), 401
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/user/profile', methods=['POST'])
def api_profile():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        return jsonify({"user": {"id":u[0], "nickname":u[1], "profile_img":u[2] or "img/profile-placeholder.png", "role":u[3]}}) if u else (jsonify({"error":"No user"}),404)
    except: return jsonify({"error":"Error"}), 500

@app.route('/api/user/update', methods=['POST'])
def api_user_update():
    try:
        uid = request.form.get('user_id'); nick = request.form.get('nickname'); file = request.files.get('profileImage')
        conn = get_db_connection(); cur = conn.cursor()
        if nick: cur.execute("UPDATE USERS SET nickname=:1 WHERE user_id=:2", [nick, uid])
        img_url = None
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{uid}_{int(datetime.now().timestamp())}.{file.filename.rsplit('.', 1)[1]}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            img_url = f"/uploads/{filename}"
            cur.execute("UPDATE USERS SET profile_img=:1 WHERE user_id=:2", [img_url, uid])
        conn.commit(); return jsonify({"message": "Updated", "image_url": img_url})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-token', methods=['GET'])
def api_token(): return jsonify({"access_token": get_spotify_headers().get('Authorization', '').split(' ')[1]})

@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.get_json(force=True, silent=True) or {}
    link = d.get('spotifyUrl'); uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "Link Error"}), 400
        res = save_track_details(tid, cur, get_spotify_headers(), [])
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [mid, tid])
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3)", [mid, tid, uid])
        conn.commit(); return jsonify({"message": "Updated", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    d = request.get_json(force=True); tags = d.get('tags', [])
    uid = d.get('user_id', 'unknown')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        for t in tags:
            t = t.strip()
            if not t: continue
            if not t.startswith('tag:'): t = f"tag:{t}"
            targets = {t}
            if skos_manager: targets.update(skos_manager.get_broader_tags(t))
            for final_tag in targets:
                try: 
                    cur.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [tid, final_tag])
                    cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, new_value, user_id) VALUES ('TRACK_TAG', :1, 'ADD', :2, :3)", [tid, final_tag, uid])
                except: pass
        conn.commit(); return jsonify({"message": "Saved"})
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        return jsonify([r[0].replace('tag:', '') for r in cursor.fetchall()])
    except: return jsonify([])

@app.route('/api/track/<track_id>.ttl', methods=['GET'])
def get_track_detail_ttl(track_id):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views FROM TRACKS WHERE track_id=:1", [track_id])
        row = cur.fetchone()
        if not row: return "Not Found", 404
        cur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :1", [track_id])
        tags = [r[0] for r in cur.fetchall()]
        tag_str = ", ".join(tags) if tags else "tag:Music"
        ttl = f"""@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n<https://knowledgemap.kr/resource/track/{track_id}> a schema:MusicRecording ;\n    schema:name "{row[0]}" ;\n    schema:byArtist "{row[1]}" ;\n    schema:image "{row[4]}" ;\n    komc:playCount "{row[8]}"^^<http://www.w3.org/2001/XMLSchema#integer> ;\n    komc:relatedTag {tag_str} ."""
        return make_response(ttl, 200, {'Content-Type': 'text/turtle; charset=utf-8'})
    except Exception as e: return str(e), 500

@app.route('/uploads/<path:filename>')
def uploaded_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)