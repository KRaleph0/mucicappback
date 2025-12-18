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
    skos_manager = SkosManager("new_data.ttl")
    print("✅ SKOS Manager Loaded Successfully (from new_data.ttl).")
except Exception as e:
    print(f"⚠️ SKOS Load Error: {e}")
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
# 1. 관리자 & 로그 API (밴 기능 추가됨)
# =========================================================
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

# [NEW] 유저 밴/언밴 API
@app.route('/api/admin/ban', methods=['POST'])
def api_ban_user():
    d = request.get_json(force=True)
    admin_id = d.get('admin_id')
    target_user_id = d.get('target_user_id')
    
    try:
        conn = get_db_connection(); cur = conn.cursor()
        
        # 1. 관리자 권한 확인
        cur.execute("SELECT role FROM USERS WHERE user_id=:1", [admin_id])
        row = cur.fetchone()
        if not row or row[0] != 'admin':
            return jsonify({"error": "관리자 권한이 필요합니다."}), 403

        # 2. 대상 유저 상태 토글
        cur.execute("SELECT is_banned FROM USERS WHERE user_id=:1", [target_user_id])
        target = cur.fetchone()
        if not target: return jsonify({"error": "유저를 찾을 수 없습니다."}), 404
        
        new_status = 1 if target[0] == 0 else 0
        cur.execute("UPDATE USERS SET is_banned=:1 WHERE user_id=:2", [new_status, target_user_id])
        
        # 로그 기록
        action = "BAN" if new_status == 1 else "UNBAN"
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, new_value, user_id) VALUES ('USER_BAN', :1, :2, :3, :4)", 
                    [target_user_id, action, str(new_status), admin_id])
        
        conn.commit()
        msg = f"유저의 권한을 {'박탈(차단)' if new_status==1 else '복구'}했습니다."
        return jsonify({"message": msg, "new_status": new_status})
        
    except Exception as e: return jsonify({"error": str(e)}), 500

# [NEW] 곡별 태그 수정 로그 조회 (관리자용, 상세 팝업용)
@app.route('/api/track/<tid>/logs', methods=['GET'])
def get_track_logs(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        sql = """
            SELECT l.created_at, u.user_id, u.nickname, u.is_banned, l.action_type, l.new_value
            FROM MODIFICATION_LOGS l
            JOIN USERS u ON l.user_id = u.user_id
            WHERE l.target_type = 'TRACK_TAG' AND l.target_id = :1
            ORDER BY l.created_at DESC
        """
        cur.execute(sql, [tid])
        rows = cur.fetchall()
        
        logs = [{
            "date": r[0].strftime("%Y-%m-%d %H:%M"),
            "user_id": r[1],
            "nickname": r[2],
            "is_banned": r[3],
            "action": r[4], 
            "tag": r[5]
        } for r in rows]
        
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500


# =========================================================
# 2. 추천 및 데이터 API
# =========================================================
@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    try:
        weather = get_current_weather()
        holiday = get_today_holiday()
        target_tags = []
        message = ""

        if holiday:
            message = f"오늘은 {holiday}! 이런 분위기 어때요?"
            target_tags = [holiday, "파티", "기념일"]
        else:
            message = f"현재 날씨({weather})에 딱 맞는 무드"
            target_tags = skos_manager.get_weather_tags(weather) if skos_manager else ["휴식", "기분전환"]

        recommended_tracks = []
        try:
            conn = get_db_connection(); cur = conn.cursor()
            search_tags = [f"tag:{t}" for t in target_tags] if target_tags else ["tag:기분전환"]
            bind_names = [f":t{i}" for i in range(len(search_tags))]
            bind_dict = {f"t{i}": t for i, t in enumerate(search_tags)}
            
            sql = f"""
                SELECT DISTINCT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
                FROM TRACKS t
                JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
                WHERE LOWER(tt.tag_id) IN ({','.join(['LOWER(' + b + ')' for b in bind_names])})
                ORDER BY DBMS_RANDOM.VALUE
                FETCH FIRST 4 ROWS ONLY
            """
            cur.execute(sql, bind_dict)
            rows = cur.fetchall()
            for r in rows:
                recommended_tracks.append({ "id": r[0], "name": r[1], "artists": [{"name": r[2]}], "album": { "images": [{"url": r[3] or "img/playlist-placeholder.png"}] }, "preview_url": r[4] })
        except: pass

        return jsonify({ "message": message, "weather": weather, "holiday": holiday, "tags": target_tags, "tracks": recommended_tracks })
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
# 3. 검색 API
# =========================================================
@app.route('/api/search', methods=['GET'])
def api_search():
    q = request.args.get('q', ''); offset = int(request.args.get('offset', '0'))
    if not q: return jsonify({"error": "No query"}), 400
    db_items = []
    
    if q.startswith('tag:'):
        try:
            tag_keyword = q.replace('tag:', '').strip(); original_tag_clean = tag_keyword.lower()
            search_tags = [tag_keyword]
            if skos_manager:
                expanded = skos_manager.get_narrower_tags(tag_keyword)
                if expanded: search_tags = expanded

            conn = get_db_connection(); cur = conn.cursor()
            final_search_terms = [f"tag:{t}" for t in search_tags]
            bind_names = [f":t{i}" for i in range(len(final_search_terms))]
            bind_dict = {f"t{i}": t for i, t in enumerate(final_search_terms)}
            
            sql = f"""
                SELECT DISTINCT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url, t.views, tt.tag_id
                FROM TRACKS t 
                JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
                WHERE LOWER(tt.tag_id) IN ({','.join(['LOWER(' + b + ')' for b in bind_names])})
            """
            cur.execute(sql, bind_dict); rows = cur.fetchall()
            temp_tracks = {}
            for r in rows:
                tid = r[0]; current_tag_suffix = r[6].replace('tag:', '').lower(); views = r[5] or 0
                score = views + (10_000_000_000 if current_tag_suffix == original_tag_clean else 0)
                if tid not in temp_tracks or score > temp_tracks[tid]['score']:
                    temp_tracks[tid] = { "data": { "id": r[0], "name": f"[추천] {r[1]}", "artists": [{"name": r[2]}], "album": { "name": "Unknown", "images": [{"url": r[3] or "img/playlist-placeholder.png"}] }, "preview_url": r[4], "external_urls": {"spotify": f"http://googleusercontent.com/spotify.com/{r[0]}"} }, "score": score }
            sorted_tracks = sorted(temp_tracks.values(), key=lambda x: x['score'], reverse=True)
            db_items = [t['data'] for t in sorted_tracks]
        except Exception as e: print(f"❌ DB Search Error: {e}")

    spotify_items = []
    try:
        headers = get_spotify_headers(); params = {"q": q, "type": "track", "limit": "20", "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        if res.status_code == 200: spotify_items = res.json().get('tracks', {}).get('items', [])
    except: pass

    seen_ids = set(); final_items = []
    for item in db_items:
        if item['id'] not in seen_ids: final_items.append(item); seen_ids.add(item['id'])
    for item in spotify_items:
        if item['id'] not in seen_ids: final_items.append(item); seen_ids.add(item['id'])
    return jsonify({ "tracks": { "items": final_items, "total": len(final_items), "offset": offset } })

# =========================================================
# 4. 유저 및 태그 관리 API
# =========================================================
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.get_json(force=True)
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role, is_banned) VALUES (:1, :2, :3, 'user', 0)", [d['id'], generate_password_hash(d['password']), d['nickname']])
        conn.commit(); return jsonify({"message": "Success"})
    except: return jsonify({"error": "Fail"}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True)
    try:
        conn = get_db_connection(); cur = conn.cursor()
        # 로그인 시 is_banned 정보는 안 보내도 되지만, 확인용으로 사용 가능
        cur.execute("SELECT user_id, password, nickname, profile_img, role, is_banned FROM USERS WHERE user_id=:1", [d['id']])
        u = cur.fetchone()
        if u and check_password_hash(u[1], d['password']): 
            return jsonify({"message":"OK", "user": {"id":u[0], "nickname":u[2], "profile_img":u[3], "role":u[4], "is_banned":u[5]}})
        return jsonify({"error": "Invalid"}), 401
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/user/profile', methods=['POST'])
def api_profile():
    d = request.get_json(force=True); uid = d.get('user_id')
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


# [API] 영화 OST 수정 (NameError 수정 완료)
@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    print(f"\n🚀 [DEBUG] OST 수정 요청 도착! (입력된 mid: {mid})") # 로그

    try:
        d = request.get_json(force=True)
        link = d.get('spotifyUrl') or d.get('url')
        uid = d.get('user_id')
        
        print(f"   -> 받은 데이터: link='{link}', user='{uid}'") # 로그

        if not link: return jsonify({"error": "URL이 없습니다."}), 400

        # 1. Movie ID 디코딩
        movie_id = mid
        try:
            padded = mid + '=' * (-len(mid) % 4)
            decoded = base64.urlsafe_b64decode(padded).decode()
            if decoded.isdigit() or len(decoded) < len(mid):
                movie_id = decoded
                print(f"   -> 🔓 Base64 디코딩 성공: {mid} => {movie_id}")
            else:
                print(f"   -> 🔒 디코딩 결과가 이상함. 원본 사용: {mid}")
        except:
            print(f"   -> 🔒 Base64 아님. 원본 사용: {mid}")

        conn = get_db_connection()
        cur = conn.cursor()

        # 2. 트랙 ID 추출
        tid = extract_spotify_id(link)
        if not tid: 
            print("   -> ❌ 트랙 ID 추출 실패")
            return jsonify({"error": "잘못된 Spotify 링크입니다."}), 400

        # 3. 트랙 정보 저장 (🚨 여기가 문제였음! services. 제거)
        print(f"   -> 🎵 트랙 정보 조회/저장 시도: {tid}")
        
        # [수정] services.save_track_details -> save_track_details
        res = save_track_details(tid, cur, get_spotify_headers(), [])
        
        if not res: 
            print("   -> ❌ 트랙 정보를 못 가져옴 (Spotify API 오류?)")
            return jsonify({"error": "트랙 정보를 가져오지 못했습니다."}), 404

        track_name = res.get('name', 'Unknown')
        print(f"   -> ✅ 트랙 확보 완료: {track_name}")

        # 4. DB 연결 (MERGE)
        print(f"   -> 💾 DB 저장 시작 (Movie: {movie_id}, Track: {tid})")
        cur.execute("""
            MERGE INTO MOVIE_OSTS m
            USING DUAL ON (m.movie_id = :mid)
            WHEN MATCHED THEN
                UPDATE SET track_id = :tid
            WHEN NOT MATCHED THEN
                INSERT (movie_id, track_id) VALUES (:mid, :tid)
        """, {'mid': movie_id, 'tid': tid})
        
        # 로그 테이블에도 기록
        cur.execute("""
            INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) 
            VALUES ('MOVIE_OST', :1, 'UPDATE', 'Unknown', :2, :3)
        """, [movie_id, f"Track:{track_name}", uid])

        conn.commit()
        print("   -> ✨ 모든 과정 성공! 응답 전송.\n")
        
        cur.close()
        conn.close()

        return jsonify({"message": "OST가 성공적으로 변경되었습니다.", "new_track": track_name})

    except Exception as e:
        print(f"❌ [CRITICAL ERROR] 처리 중 예외 발생: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# [수정] 태그 추가 API (밴 여부 체크)
@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    d = request.get_json(force=True); tags = d.get('tags', [])
    uid = d.get('user_id', 'unknown')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        
        # 밴 여부 확인
        cur.execute("SELECT is_banned FROM USERS WHERE user_id=:1", [uid])
        user_row = cur.fetchone()
        if user_row and user_row[0] == 1:
            return jsonify({"error": "태그 편집 권한이 박탈된 계정입니다. 관리자에게 문의하세요."}), 403

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
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>/tags', methods=['DELETE'])
def api_delete_tag(tid):
    d = request.get_json(force=True)
    tag_to_delete = d.get('tag')
    uid = d.get('user_id')

    try:
        conn = get_db_connection(); cur = conn.cursor()

        # 1. 유저 권한 확인 (밴 여부)
        cur.execute("SELECT is_banned, role FROM USERS WHERE user_id=:1", [uid])
        user_row = cur.fetchone()
        if not user_row: return jsonify({"error": "유저 정보 없음"}), 404
        if user_row[0] == 1: return jsonify({"error": "권한이 박탈된 계정입니다."}), 403

        # 2. 태그 삭제 실행
        # tag: 접두사가 없으면 붙여서 처리
        if not tag_to_delete.startswith('tag:'): tag_to_delete = f"tag:{tag_to_delete}"

        cur.execute("DELETE FROM TRACK_TAGS WHERE track_id=:1 AND tag_id=:2", [tid, tag_to_delete])
        
        if cur.rowcount > 0:
            # 3. 로그 기록 (DELETE 액션)
            cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, user_id) VALUES ('TRACK_TAG', :1, 'DELETE', :2, :3)", 
                        [tid, tag_to_delete, uid])
            conn.commit()
            return jsonify({"message": "Deleted"})
        else:
            return jsonify({"error": "태그를 찾을 수 없습니다."}), 404

    except Exception as e: return jsonify({"error": str(e)}), 500



@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        return jsonify([r[0].replace('tag:', '') for r in cur.fetchall()])
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