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

# =========================================================
<<<<<<< Updated upstream
# 1. 관리자 & 로그 API (요구사항 1번)
# =========================================================

@app.route('/api/admin/logs', methods=['GET'])
def get_admin_logs():
    """관리자용 수정 로그 조회"""
    # 실제 운영 시에는 여기서 관리자 세션 체크가 필요합니다.
    # 예: if not is_admin(request): return jsonify({"error": "Unauthorized"}), 403
    
    try:
        conn = get_db_connection(); cur = conn.cursor()
        # 최근 50개의 수정 로그 조회 (유저 닉네임 조인)
        cur.execute("""
            SELECT l.log_id, l.target_type, l.target_id, l.action_type, 
                   l.previous_value, l.new_value, l.created_at, u.nickname
            FROM MODIFICATION_LOGS l
            LEFT JOIN USERS u ON l.user_id = u.user_id
            ORDER BY l.created_at DESC
            FETCH FIRST 50 ROWS ONLY
        """)
        rows = cur.fetchall()
        
        logs = []
        for r in rows:
            logs.append({
                "id": r[0],
                "type": r[1],
                "target": r[2],
                "action": r[3],
                "prev": r[4],
                "new": r[5],
                "date": r[6].strftime("%Y-%m-%d %H:%M:%S") if r[6] else "",
                "user": r[7] or "Unknown"
            })
        return jsonify(logs)
    except Exception as e:
        print(f"[Admin Log Error] {e}")
        return jsonify({"error": str(e)}), 500

# =========================================================
# 2. 추천 및 TTL 데이터 API (요구사항 2번 - 에러 수정)
# =========================================================

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    """상황별 추천 (404 에러 해결)"""
    try:
        weather = get_current_weather()
        holiday = get_today_holiday()
        
        # 간단한 추천 로직
        msg = f"현재 날씨는 {weather}입니다."
        if holiday: msg = f"오늘은 {holiday}! 즐거운 연휴 보내세요."
        
        # 더미 데이터 반환 (프론트 오류 방지)
        return jsonify({
            "message": msg,
            "weather": weather,
            "holiday": holiday,
            "tracks": [] # 추후 추천 로직 구현 시 채움
        })
    except Exception as e:
        print(f"[Context Error] {e}")
        return jsonify({"error": str(e)}), 500
=======
# 1. 영화/TTL 데이터 API
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def admin_update_movies():
    try: return jsonify({"message": update_box_office_data()})
    except Exception as e: return jsonify({"error": str(e)}), 500
>>>>>>> Stashed changes

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    """박스오피스 TTL 생성 (500 에러 해결 - 안전한 쿼리 사용)"""
    try:
<<<<<<< Updated upstream
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # LEFT JOIN을 사용하여 OST 정보가 없어도 영화 정보는 출력되도록 수정
        cursor.execute("""
=======
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
>>>>>>> Stashed changes
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            ORDER BY m.rank ASC
        """)
        rows = cur.fetchall()
        
<<<<<<< Updated upstream
        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "",
            "# Box Office Data"
        ]
        
        seen_movies = set()
        for r in rows:
            mid_raw, title, rank, poster = r[0], r[1], r[2], r[3]
            tid, ttitle, artist, tcover = r[4], r[5], r[6], r[7]
            
            if not mid_raw or title in seen_movies: continue
            seen_movies.add(title)
            
            mid = base64.urlsafe_b64encode(str(mid_raw).encode()).decode().rstrip("=")
            img = poster or "img/playlist-placeholder.png"
            
            ttl_parts.append(f"""
<https://knowledgemap.kr/resource/movie/{mid}> a schema:Movie ;
    schema:name "{title}" ;
    komc:rank {rank} ;
    schema:image "{img}" .""")

            if tid:
                ttl_parts.append(f"""
<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ;
    schema:name "{ttitle}" ;
    schema:byArtist "{artist}" ;
    schema:image "{tcover or img}" ;
    komc:featuredIn <https://knowledgemap.kr/resource/movie/{mid}> .""")

        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        print(f"[TTL Error] {e}")
        # 에러가 나도 서버가 죽지 않고 에러 메시지를 텍스트로 반환
        return make_response(f"# Error generating TTL: {str(e)}", 200, {'Content-Type': 'text/turtle'})

# =========================================================
# 3. 검색 API (요구사항 3번 - 태그 검색 우선순위)
# =========================================================

=======
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        seen = set()
        for r in rows:
            mid_raw, title, rank, poster = r[0], r[1], r[2], r[3]
            if not mid_raw or title in seen: continue
            seen.add(title)
            
            mid = base64.urlsafe_b64encode(str(mid_raw).encode()).decode().rstrip("=")
            img = poster or r[7] or "img/playlist-placeholder.png"
            tid = r[4] or f"{mid}_ost"
            ttl += f"""<https://knowledgemap.kr/resource/movie/{mid}> a schema:Movie ; schema:name "{title}" ; schema:image "{img}" ; komc:rank {rank} .\n<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ; schema:name "{r[5] or 'OST 정보 없음'}" ; schema:byArtist "{r[6] or 'Unknown'}" ; schema:image "{img}" ; komc:featuredIn <https://knowledgemap.kr/resource/movie/{mid}> .\n"""
        return Response(ttl, mimetype='text/turtle')
    except: return Response("# Error", mimetype='text/turtle')

# =========================================================
# 2. [핵심] 검색 API (태그 우선 검색 복구)
# =========================================================
>>>>>>> Stashed changes
@app.route('/api/search', methods=['GET'])
def api_search():
    q = request.args.get('q', '')
    offset = request.args.get('offset', '0')
    
    if not q: return jsonify({"error": "No query"}), 400

<<<<<<< Updated upstream
    # [핵심] 태그 검색(tag:...)인 경우 로컬 DB를 최우선으로 검색
    if q.startswith('tag:'):
        target_tag = q.strip()
        try:
            print(f"🔎 [Search] 태그 우선 검색: {target_tag}")
            conn = get_db_connection(); cur = conn.cursor()
            
            # 태그가 일치하는 곡을 조회수(views) 높은 순으로 가져옴
=======
    # [1] 태그 검색이면 -> 로컬 DB 우선 조회
    if q.startswith('tag:'):
        target_tag = q.strip()
        try:
            print(f"🔎 [Search] 태그 검색 시도: {target_tag}")
            conn = get_db_connection(); cur = conn.cursor()
            
            # 태그가 달린 곡들을 조회수(views) 순으로 가져옴
>>>>>>> Stashed changes
            cur.execute("""
                SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url, 
                       a.album_title, a.album_id
                FROM TRACKS t
                JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
                LEFT JOIN ALBUMS a ON t.album_id = a.album_id
                WHERE tt.tag_id = :tag
                ORDER BY t.views DESC
            """, [target_tag])
            rows = cur.fetchall()
            
<<<<<<< Updated upstream
            # Spotify 포맷으로 변환하여 반환 (프론트엔드 호환성)
=======
            # Spotify 포맷으로 변환 (프론트엔드 호환성 유지)
>>>>>>> Stashed changes
            items = []
            for r in rows:
                items.append({
                    "id": r[0],
                    "name": r[1],
                    "artists": [{"name": r[2]}],
                    "album": {
<<<<<<< Updated upstream
                        "name": r[5] or "Unknown",
                        "images": [{"url": r[3] or "img/playlist-placeholder.png"}]
                    },
                    "preview_url": r[4],
                    "external_urls": {"spotify": f"http://googleusercontent.com/spotify.com/{r[0]}"}
                })
            
            # DB 결과가 있으면 바로 반환 (Spotify 검색 안 함 -> 태그 결과가 최상위 노출됨)
            if items:
                print(f"✅ [Search] DB에서 {len(items)}곡 발견")
                return jsonify({"tracks": {"items": items}})
            else:
                print("⚠️ [Search] DB에 해당 태그 없음, Spotify 검색으로 전환")

        except Exception as e:
            print(f"❌ [Search Error] {e}")

    # [기존] 일반 검색 또는 DB에 태그가 없을 경우 Spotify API 사용
=======
                        "name": r[5] or "Unknown Album",
                        "images": [{"url": r[3] or "img/playlist-placeholder.png"}],
                        "release_date": ""
                    },
                    "preview_url": r[4],
                    "external_urls": {"spotify": f"https://open.spotify.com/track/{r[0]}"}
                })
            
            print(f"✅ [Search] 태그 결과 {len(items)}건 발견")
            return jsonify({"tracks": {"items": items}})

        except Exception as e:
            print(f"❌ [Search Error] {e}")
            # 에러 시 빈 결과 반환

    # [2] 일반 검색 -> Spotify API
>>>>>>> Stashed changes
    try:
        headers = get_spotify_headers()
        params = {"q": q, "type": "track,album,artist", "limit": "20", "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        return jsonify(res.json())
    except Exception as e: return jsonify({"error": str(e)}), 500

# =========================================================
<<<<<<< Updated upstream
# 4. 유저 및 기타 API
# =========================================================

=======
# 3. 유저 & 프로필 API
# =========================================================
>>>>>>> Stashed changes
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
        if u and check_password_hash(u[1], pw): 
<<<<<<< Updated upstream
            return jsonify({"message":"OK", "user": {"id":u[0], "nickname":u[2], "profile_img":u[3], "role":u[4]}}) # role 반환 필수
=======
            return jsonify({"message":"OK", "user": {"id":u[0], "nickname":u[2], "profile_img":u[3], "role":u[4]}})
>>>>>>> Stashed changes
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
        uid = request.form.get('user_id') or request.json.get('user_id')
        nick = request.form.get('nickname') or request.json.get('nickname')
        file = request.files.get('profileImage')
        
        conn = get_db_connection(); cur = conn.cursor()
        if nick: cur.execute("UPDATE USERS SET nickname=:1 WHERE user_id=:2", [nick, uid])
        img_url = None
        if file and allowed_file(file.filename):
            filename = secure_filename(f"{uid}_{int(datetime.now().timestamp())}.{file.filename.rsplit('.', 1)[1]}")
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            img_url = f"/uploads/{filename}"
            cur.execute("UPDATE USERS SET profile_img=:1 WHERE user_id=:2", [img_url, uid])
        conn.commit()
        return jsonify({"message": "Updated", "image_url": img_url})
    except Exception as e: return jsonify({"error": str(e)}), 500

<<<<<<< Updated upstream
@app.route('/api/admin/update-movies', methods=['POST'])
def admin_update_movies():
    try: return jsonify({"message": update_box_office_data()})
    except Exception as e: return jsonify({"error": str(e)}), 500

=======
# =========================================================
# 4. 기타 API
# =========================================================
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
        
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [mid, tid])
        
        # [로그 남기기] 요구사항 1번을 위해 필수
        cur.execute("""
            INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) 
            VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3)
        """, [mid, tid, uid])
        
=======
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [mid, tid])
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3)", [mid, tid, uid])
>>>>>>> Stashed changes
        conn.commit()
        return jsonify({"message": "Updated", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    d = request.get_json(force=True); tags = d.get('tags', [])
<<<<<<< Updated upstream
    uid = d.get('user_id', 'anonymous')
=======
>>>>>>> Stashed changes
    try:
        conn = get_db_connection(); cur = conn.cursor()
        for t in tags:
            t = t.strip()
            if not t: continue
            if not t.startswith('tag:'): t = f"tag:{t}"
            targets = {t}
            if skos_manager: targets.update(skos_manager.get_broader_tags(t))
            for final_tag in targets:
<<<<<<< Updated upstream
                try: 
                    cur.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [tid, final_tag])
                    # [로그 남기기]
                    cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, new_value, user_id) VALUES ('TRACK_TAG', :1, 'ADD', :2, :3)", [tid, final_tag, uid])
=======
                try: cur.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [tid, final_tag])
>>>>>>> Stashed changes
                except: pass
        conn.commit()
        return jsonify({"message": "Tags Saved"})
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        return jsonify([r[0].replace('tag:', '') for r in cursor.fetchall()])
    except: return jsonify([])

@app.route('/uploads/<path:filename>')
def uploaded_file(filename): return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
