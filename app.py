import os
import requests
import oracledb
import base64
import re
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# 모듈 import
from config import UPLOAD_FOLDER, SPOTIFY_API_BASE
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data, save_track_details
from utils import get_spotify_headers, get_current_weather, get_today_holiday, extract_spotify_id

# [선택] SKOS 매니저
try:
    from skos_manager import SkosManager
    skos_manager = SkosManager("skos-definition.ttl")
except:
    skos_manager = None

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CORS(app)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db_pool()

# =========================================================
# 1. 영화/TTL API (중복 제거 & 안전장치)
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def admin_update_movies():
    return jsonify({"message": update_box_office_data()})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 랭킹 순 조회
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            WHERE m.rank <= 10
            ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        
        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .",
            ""
        ]
        
        seen_titles = set() # [핵심] 중복 방지

        for r in rows:
            # 데이터 추출 (None 처리)
            mid_raw, title, rank, poster = r[0], r[1], r[2], r[3]
            tid, t_title, t_artist, t_cover, preview = r[4], r[5], r[6], r[7], r[8]

            if not mid_raw or title in seen_titles: continue
            seen_titles.add(title)

            # ID 인코딩
            mid = base64.urlsafe_b64encode(str(mid_raw).encode()).decode().rstrip("=")
            final_poster = poster or t_cover or "img/playlist-placeholder.png"

            # 영화 정보
            ttl_parts.append(f"""
<https://knowledgemap.kr/komc/resource/movie/{mid}> a schema:Movie ;
    schema:name "{title}" ;
    schema:image "{final_poster}" ;
    komc:rank {rank} .""")

            # 트랙 정보 (없으면 '정보 없음'으로 표시)
            if tid:
                t_uri = tid
                t_name = t_title
                t_art = t_artist
            else:
                t_uri = f"{mid}_ost"
                t_name = f"{title} (OST 정보 없음)"
                t_art = "Unknown"

            ttl_parts.append(f"""
<https://knowledgemap.kr/komc/resource/track/{t_uri}> a schema:MusicRecording ;
    schema:name "{t_name}" ;
    schema:byArtist "{t_art}" ;
    schema:image "{final_poster}" ;
    schema:audio "{preview or ''}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{mid}> ;
    schema:genre "Movie Soundtrack" .""")

        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        print(f"[TTL Error] {e}")
        return make_response("# Error generating TTL", 200, {'Content-Type': 'text/turtle'})

# =========================================================
# 2. 유저 매칭 & 태그 API (필수 추가)
# =========================================================

# [NEW] 유저가 OST 직접 연결
@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.get_json(force=True, silent=True) or {}
    link = d.get('spotifyUrl')
    uid = d.get('user_id')
    
    if not link: return jsonify({"error": "링크가 없습니다."}), 400

    try:
        conn = get_db_connection(); cur = conn.cursor()
        
        # 1. 영화 ID 복원 (필요시 디코딩 로직 추가, 지금은 그대로 사용 가정)
        # 만약 mid가 base64라면 디코딩해야 함. 여기선 KOBIS 코드가 그대로 온다고 가정.
        # 하지만 프론트에서 openEditModal에 넘기는 ID 확인 필요. 
        # (서비스 로직상 update_box_office_data에서 movieCd를 썼으므로 그대로 씀)
        
        # 2. Spotify ID 추출
        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "잘못된 Spotify 링크입니다."}), 400

        # 3. 트랙 정보 저장 & 태그 생성
        headers = get_spotify_headers()
        track_info = save_track_details(tid, cur, headers, []) # 여기서 태그도 저장됨!
        
        if not track_info: return jsonify({"error": "트랙 정보를 찾을 수 없습니다."}), 404

        # 4. 연결 테이블 갱신
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [mid, tid])
        
        # 5. 로그 남기기
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3)", [mid, tid, uid])
        
        conn.commit()
        return jsonify({"message": f"'{track_info['name']}' 곡으로 등록되었습니다!", "new_track": track_info['name']})
    except Exception as e:
        print(e)
        return jsonify({"error": "서버 오류 발생"}), 500

# [NEW] 태그 추가
@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    d = request.get_json(force=True)
    tags = d.get('tags', [])
    if not tags: return jsonify({"message": "태그 없음"})
    
    try:
        conn = get_db_connection(); cur = conn.cursor()
        for tag in tags:
            tag = tag.strip()
            if not tag: continue
            if not tag.startswith('tag:'): tag = f"tag:{tag}"
            
            # 확장
            targets = {tag}
            if skos_manager: targets.update(skos_manager.get_broader_tags(tag))
            
            for t in targets:
                try: cursor.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [tid, t])
                except: pass
        conn.commit()
        return jsonify({"message": "태그 저장 완료"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        return jsonify([r[0].replace('tag:', '') for r in cursor.fetchall()])
    except: return jsonify([])

# =========================================================
# 3. 인증 & 기타 API
# =========================================================
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.get_json(force=True); uid = d.get('id'); pw = d.get('password'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except: return jsonify({"error": "Fail"}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True); uid = d.get('id'); pw = d.get('password')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"user": {"id":u[0], "nickname":u[2], "profile_img":u[3]}})
        return jsonify({"error": "Invalid"}), 401
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/user/profile', methods=['POST'])
def api_profile():
    d = request.get_json(force=True); uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        return jsonify({"user": {"id":u[0], "nickname":u[1], "profile_img":u[2] or "img/profile-placeholder.png"}}) if u else (jsonify({"error":"No user"}),404)
    except: return jsonify({"error":"Error"}), 500

@app.route('/api/user/update', methods=['POST'])
def api_user_update():
    d = request.get_json(force=True); uid = d.get('user_id'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE USERS SET nickname=:1 WHERE user_id=:2", [nick, uid])
        conn.commit(); return jsonify({"message": "Updated"})
    except: return jsonify({"error": "Error"}), 500

# (Spotify Token, Search 등 나머지 유지)
@app.route('/api/spotify-token', methods=['GET'])
def api_token(): return jsonify({"access_token": get_spotify_headers().get('Authorization', '').split(' ')[1]})

@app.route('/api/search', methods=['GET'])
def api_src():
    return jsonify(requests.get(f"{SPOTIFY_API_BASE}/search", headers=get_spotify_headers(), params={"q":request.args.get('q'),"type":"track","limit":20,"market":"KR"}).json())

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)