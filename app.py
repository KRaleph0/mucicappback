import os
import requests # requests 모듈 필요
import oracledb
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime

# 모듈 import
from config import UPLOAD_FOLDER, SPOTIFY_API_BASE
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data
from utils import allowed_file, verify_turnstile, get_spotify_headers

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

# 업로드 폴더 자동 생성
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS(app)

# DB 연결 해제 핸들러
app.teardown_appcontext(close_db)

# 서버 시작 시 DB 풀 생성
with app.app_context():
    init_db_pool()

# =========================================================
# 1. 인증 (Auth) API
# =========================================================
@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    nickname = data.get('nickname')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid: return jsonify({"error": err_msg}), 400

    if not all([user_id, password, nickname]):
        return jsonify({"error": "모든 필드를 입력해주세요."}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM USERS WHERE user_id = :1", [user_id])
        if cursor.fetchone(): return jsonify({"error": "이미 존재하는 아이디입니다."}), 409

        cursor.execute("INSERT INTO USERS (user_id, password, nickname, profile_img) VALUES (:1, :2, :3, :4)", [user_id, password, nickname, None])
        conn.commit()
        return jsonify({"message": "회원가입 성공"}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid: return jsonify({"error": err_msg}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT nickname, profile_img FROM USERS WHERE user_id = :1 AND password = :2", [user_id, password])
        row = cursor.fetchone()
        if row:
            return jsonify({"message": "로그인 성공", "user": {"user_id": user_id, "nickname": row[0], "profile_img": row[1]}}), 200
        else:
            return jsonify({"error": "아이디 또는 비밀번호가 잘못되었습니다."}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# =========================================================
# 2. 사용자 (User) API
# =========================================================
@app.route('/api/user/profile', methods=['GET', 'POST'])
def handle_profile():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as e: return jsonify({"error": str(e)}), 500

    if request.method == 'GET':
        user_id = request.args.get('user_id')
        if not user_id: return jsonify({"error": "User ID required"}), 400
        try:
            cursor.execute("SELECT nickname, profile_img FROM USERS WHERE user_id = :1", [user_id])
            row = cursor.fetchone()
            if row: return jsonify({"nickname": row[0], "profile_img": row[1]})
            else: return jsonify({"error": "User not found"}), 404
        except Exception as e: return jsonify({"error": str(e)}), 500

    if request.method == 'POST':
        try:
            user_id = request.form.get('user_id')
            nickname = request.form.get('nickname')
            file = request.files.get('profileImage')

            if nickname:
                cursor.execute("UPDATE USERS SET nickname = :1 WHERE user_id = :2", [nickname, user_id])

            web_path = None
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
                # 웹 접근 경로 저장 (/uploads/파일명)
                web_path = f"/uploads/{filename}"
                cursor.execute("UPDATE USERS SET profile_img = :1 WHERE user_id = :2", [web_path, user_id])

            conn.commit()
            return jsonify({"message": "저장 완료", "image_url": web_path})
        except Exception as e:
            if 'conn' in locals(): conn.rollback()
            return jsonify({"error": str(e)}), 500

@app.route('/api/user/password', methods=['POST'])
def update_password():
    data = request.json
    user_id = data.get('user_id')
    current_pw = data.get('currentPassword')
    new_pw = data.get('newPassword')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid: return jsonify({"error": err_msg}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM USERS WHERE user_id = :1", [user_id])
        row = cursor.fetchone()
        if not row or row[0] != current_pw: return jsonify({"error": "현재 비밀번호 불일치"}), 400

        cursor.execute("UPDATE USERS SET password = :1 WHERE user_id = :2", [new_pw, user_id])
        conn.commit()
        return jsonify({"message": "비밀번호 변경 완료"})
    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        return jsonify({"error": str(e)}), 500

# =========================================================
# 3. 기타 기능 (박스오피스, 추천)
# =========================================================
@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    try:
        msg = update_box_office_data()
        return jsonify({"message": msg})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/recommend/weather', methods=['GET'])
def api_recommend_weather():
    try:
        condition = request.args.get('condition', 'Clear')
        tag_map = {'Clear': 'tag:Clear', 'Rain': 'tag:Rain', 'Snow': 'tag:Snow', 'Clouds': 'tag:Cloudy'}
        target_tag = tag_map.get(condition, 'tag:Clear')

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.track_title, t.preview_url, a.album_cover_url, m.title
            FROM TRACKS t
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            JOIN ALBUMS a ON t.album_id = a.album_id
            LEFT JOIN MOVIE_OSTS mo ON t.track_id = mo.track_id
            LEFT JOIN MOVIES m ON mo.movie_id = m.movie_id
            WHERE tt.tag_id = :1
        """, [target_tag])
        
        results = []
        for row in cursor.fetchall():
            results.append({"title": row[0], "preview": row[1], "cover": row[2], "movie": row[3]})
        return jsonify(results)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/data/box-office.ttl')
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            JOIN TRACKS t ON mo.track_id = t.track_id
            ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        ttl_parts = ["@prefix schema: <http://schema.org/> .", "@prefix komc: <https://knowledgemap.kr/komc/def/> .", ""]
        for row in rows:
            mid, mtitle, rank, mposter, tid, ttitle, artist, tcover, audio = row
            ttl_parts.append(f"<https://knowledgemap.kr/resource/movie/{mid}> a schema:Movie ; schema:name \"{mtitle}\" ; komc:rank {rank} .")
            ttl_parts.append(f"<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ; schema:name \"{ttitle}\" ; schema:byArtist \"{artist}\" ; komc:featuredIn <https://knowledgemap.kr/resource/movie/{mid}> .")
        
        response = make_response("\n".join(ttl_parts))
        response.headers['Content-Type'] = 'text/turtle; charset=utf-8'
        return response
    except Exception as e: return str(e), 500

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    return jsonify({"message": "오늘의 추천", "tracks": []}) # 간단한 더미 응답 (오류 방지용)

# =========================================================
# [중요] 4. 검색 프록시 API (Spotify 검색 중계)
# =========================================================
@app.route('/api/search', methods=['GET'])
def proxy_search():
    """프론트엔드 대신 Spotify Search API를 호출하고 결과를 반환"""
    try:
        # 1. 프론트에서 보낸 파라미터 받기
        query = request.args.get('q')
        search_type = request.args.get('type', 'track')
        limit = request.args.get('limit', '20')
        offset = request.args.get('offset', '0')
        market = request.args.get('market', 'KR')

        if not query:
            return jsonify({"error": "검색어가 없습니다."}), 400

        # 2. Spotify 토큰 발급 (utils.py 활용)
        headers = get_spotify_headers()

        # 3. Spotify API 호출
        params = {
            "q": query,
            "type": search_type,
            "limit": limit,
            "offset": offset,
            "market": market
        }
        response = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        
        # 4. 결과 반환
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify(response.json()), response.status_code

    except Exception as e:
        print(f"[Search Proxy Error] {e}")
        return jsonify({"error": "서버 내부 오류"}), 500

# =========================================================
# [중요] 5. 업로드된 이미지 파일 제공 (라우트 추가)
# =========================================================
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    """/uploads/파일명.png 요청 시 실제 파일 반환"""
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)