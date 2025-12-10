# app.py
import os
import base64
import uuid
import requests
from flask import Flask, request, jsonify, Response, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# 분리된 모듈 import
import config
import database
import utils
import services

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.UPLOAD_FOLDER
CORS(app)

# DB 풀 초기화
try:
    database.init_db_pool()
except:
    pass

@app.teardown_appcontext
def shutdown_session(exception=None):
    database.close_db()

# --- 라우트 정의 ---

@app.route('/api/recommend/context', methods=['GET'])
def api_recommend_context():
    try:
        weather = utils.get_current_weather() or "Clear"
        holiday = utils.get_today_holiday()
        
        target_tags = []
        context_msg = ""

        if holiday:
            context_msg = f"🎉 오늘은 {holiday}입니다! 신나는 음악 어때요?"
            target_tags = ['tag:Exciting', 'tag:Pop']
        elif weather == "Rain":
            context_msg = "☔ 비가 오네요. 감성적인 음악을 준비했어요."
            target_tags = ['tag:Sentimental', 'tag:Rest']
        elif weather == "Snow":
            context_msg = "❄️ 눈이 내립니다. 로맨틱한 음악을 들어보세요."
            target_tags = ['tag:Romance', 'tag:Sentimental']
        else:
            context_msg = "☀️ 맑은 날씨엔 드라이브 음악이죠!"
            target_tags = ['tag:Exciting', 'tag:Pop']

        conn = database.get_db_connection()
        cursor = conn.cursor()
        
        bind_vars = {f't{i}': t for i, t in enumerate(target_tags)}
        placeholders = ', '.join([f':t{i}' for i in range(len(target_tags))])
        
        query = f"""
            SELECT t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM TRACKS t
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            WHERE tt.tag_id IN ({placeholders})
            ORDER BY DBMS_RANDOM.VALUE
            FETCH FIRST 6 ROWS ONLY
        """
        cursor.execute(query, bind_vars)
        
        tracks = []
        for row in cursor.fetchall():
            tracks.append({"title": row[0], "artist": row[1], "cover": row[2], "preview": row[3]})
            
        return jsonify({
            "message": context_msg,
            "weather": weather,
            "holiday": holiday,
            "tracks": tracks,
            "tags": [t.replace('tag:', '') for t in target_tags]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/user/profile-image', methods=['POST'])
def upload_profile_image():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    user_id = request.form.get('user_id')
    if file and utils.allowed_file(file.filename) and user_id:
        try:
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"{user_id}_{uuid.uuid4().hex[:8]}.{ext}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            image_url = f"/uploads/{filename}"
            conn = database.get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE USERS SET profile_img = :1 WHERE user_id = :2", [image_url, user_id])
            conn.commit()
            return jsonify({"message": "OK", "image_url": image_url})
        except Exception as e: return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Invalid request"}), 400

@app.route('/uploads/<name>')
def download_file(name):
    return send_from_directory(app.config["UPLOAD_FOLDER"], name)

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json; uid, pw, nick = d.get('id'), d.get('password'), d.get('nickname', 'User')
    try:
        conn = database.get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cur.fetchone(): return jsonify({"error": "ID exists"}), 409
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json; uid, pw = d.get('id'), d.get('password')
    try:
        conn = database.get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"message": "Login success", "user": {"id": u[0], "nickname": u[2], "profile_img": u[3], "role": u[4]}})
        return jsonify({"error": "Invalid"}), 401
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/logs', methods=['POST'])
def api_logs():
    d = request.json; uid = d.get('user_id')
    try:
        conn = database.get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT role FROM USERS WHERE user_id=:1", [uid])
        res = cur.fetchone()
        if not res or res[0] != 'admin': return jsonify({"error": "No permission"}), 403
        cur.execute("SELECT target_id, previous_value, new_value, user_id, created_at, user_ip FROM MODIFICATION_LOGS ORDER BY created_at DESC FETCH FIRST 50 ROWS ONLY")
        logs = [{"movie":r[0], "old":r[1], "new":r[2], "user":r[3], "date":r[4].strftime("%Y-%m-%d %H:%M"), "ip":r[5]} for r in cur.fetchall()]
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.json; link = d.get('spotifyUrl'); uid = d.get('user_id', 'Guest'); ip = request.remote_addr
    if not link: return jsonify({"error": "Link required"}), 400
    try:
        conn = database.get_db_connection(); cur = conn.cursor(); headers = utils.get_spotify_headers()
        real_mid = mid
        try:
            if mid.endswith('_ost'): mid = mid[:-4]
            pad = len(mid)%4; 
            if pad: mid += '='*(4-pad)
            dec = base64.urlsafe_b64decode(mid).decode('utf-8')
            cur.execute("SELECT count(*) FROM MOVIES WHERE movie_id=:1", [dec])
            if cur.fetchone()[0]>0: real_mid = dec
        except: pass
        tid = utils.extract_spotify_id(link)
        if not tid: return jsonify({"error": "Invalid Link"}), 400
        res = services.save_track_details(tid, cur, headers, [])
        if not res: return jsonify({"error": "Track not found"}), 404
        cur.execute("SELECT track_id FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        prev = cur.fetchone(); prev_id = prev[0] if prev else "NONE"
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [real_mid, tid])
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_ip, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', :2, :3, :4, :5)", [real_mid, prev_id, tid, ip, uid])
        conn.commit()
        return jsonify({"message": "Updated", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_adm_update(): return jsonify({"message": services.update_box_office_data()})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = database.get_db_connection(); cur = conn.cursor()
        cur.execute("""SELECT m.movie_id, m.title, m.rank, m.poster_url, t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url FROM MOVIES m LEFT JOIN MOVIE_OSTS mo ON m.movie_id=mo.movie_id LEFT JOIN TRACKS t ON mo.track_id=t.track_id LEFT JOIN ALBUMS a ON t.album_id=a.album_id WHERE m.rank<=10 ORDER BY m.rank ASC""")
        rows = cur.fetchall()
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        tcur = conn.cursor()
        for r in rows:
            mid, mt, rk, mp, tid, tt, ar, pr, cov = r
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mp = mp or "img/playlist-placeholder.png"; cov = cov or "img/playlist-placeholder.png"; tt = tt or "OST 정보 없음"; ar = ar or "-"
            tags = ""
            if tid:
                tcur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
                tl = [x[0].replace('tag:', '') for x in tcur.fetchall()]
                if tl: tags = f"    komc:relatedTag tag:{', tag:'.join(tl)} ;"
            t_uri = tid if tid else f"{m_uri}_ost"
            ttl += f"""<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ; schema:name "{mt}" ; schema:image "{mp}" ; komc:rank {rk} .\n<https://knowledgemap.kr/komc/resource/track/{t_uri}> a schema:MusicRecording ; schema:name "{tt}" ; schema:byArtist "{ar}" ; schema:image "{cov}" ; schema:audio "{pr or ''}" ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;\n{tags}\n    schema:genre "Movie Soundtrack" .\n"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e: return f"# Error: {e}", 500

@app.route('/api/spotify-token', methods=['GET'])
def api_tk():
    try:
        return jsonify(utils.get_spotify_headers())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_src():
    query = request.args.get('q')
    search_type = request.args.get('type', 'track')
    limit = request.args.get('limit', '20')
    if not query: return jsonify({"error": "No query"}), 400
    try:
        headers = utils.get_spotify_headers()
        res = requests.get(f"{config.SPOTIFY_API_BASE}/search", headers=headers, 
                           params={"q": query, "type": search_type, "limit": limit, "market": "KR"})
        return jsonify(res.json())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>', methods=['GET'])
def api_tr(tid):
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        headers = utils.get_spotify_headers()
        res = services.save_track_details(tid, cursor, headers)
        if res:
            cursor.execute("UPDATE TRACKS SET views = views + 1 WHERE track_id = :1", [tid])
            conn.commit()
            return jsonify(res)
        return jsonify({"error": "Failed"}), 404
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)