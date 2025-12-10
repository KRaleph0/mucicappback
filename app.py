import os
import oracledb
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# 모듈 import
from config import UPLOAD_FOLDER
# [수정 1] init_db_pool 추가 임포트
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data
from utils import allowed_file, verify_turnstile

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS(app)

# [수정 2] 앱 컨텍스트 내에서 DB 연결 종료 설정
app.teardown_appcontext(close_db)

# [수정 3] 서버 시작 시 DB 풀 생성 (이게 없어서 에러가 났습니다!)
with app.app_context():
    init_db_pool()

# =========================================================
# 1. 인증 (Auth) API
# =========================================================

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    """회원가입"""
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    nickname = data.get('nickname')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    if not all([user_id, password, nickname]):
        return jsonify({"error": "모든 필드를 입력해주세요."}), 400

    # DB 연결 가져오기 (init_db_pool이 선행되어야 함)
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT user_id FROM USERS WHERE user_id = :1", [user_id])
        if cursor.fetchone():
            return jsonify({"error": "이미 존재하는 아이디입니다."}), 409

        cursor.execute(
            "INSERT INTO USERS (user_id, password, nickname, profile_img) VALUES (:1, :2, :3, :4)",
            [user_id, password, nickname, None] 
        )
        conn.commit()
        return jsonify({"message": "회원가입 성공"}), 201

    except Exception as e:
        # conn.rollback() # conn이 없을 수도 있으므로 주의
        print(f"[Signup Error] {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/auth/login', methods=['POST'])
def login():
    """로그인"""
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT nickname, profile_img FROM USERS WHERE user_id = :1 AND password = :2",
            [user_id, password]
        )
        row = cursor.fetchone()

        if row:
            return jsonify({
                "message": "로그인 성공",
                "user": {
                    "user_id": user_id,
                    "nickname": row[0],
                    "profile_img": row[1]
                }
            }), 200
        else:
            return jsonify({"error": "아이디 또는 비밀번호가 잘못되었습니다."}), 401

    except Exception as e:
        print(f"[Login Error] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 2. 사용자 (User) API
# =========================================================

@app.route('/api/user/profile', methods=['GET', 'POST'])
def handle_profile():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if request.method == 'GET':
        user_id = request.args.get('user_id')
        if not user_id: return jsonify({"error": "User ID required"}), 400
        
        try:
            cursor.execute("SELECT nickname, profile_img FROM USERS WHERE user_id = :1", [user_id])
            row = cursor.fetchone()
            if row:
                return jsonify({"nickname": row[0], "profile_img": row[1]})
            else:
                return jsonify({"error": "User not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

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
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                
                web_path = f"/uploads/{filename}"
                cursor.execute("UPDATE USERS SET profile_img = :1 WHERE user_id = :2", [web_path, user_id])

            conn.commit()
            return jsonify({
                "message": "프로필 저장 완료", 
                "image_url": web_path
            })

        except Exception as e:
            if 'conn' in locals(): conn.rollback()
            print(f"[Profile POST Error] {e}")
            return jsonify({"error": str(e)}), 500


@app.route('/api/user/password', methods=['POST'])
def update_password():
    data = request.json
    user_id = data.get('user_id')
    current_pw = data.get('currentPassword')
    new_pw = data.get('newPassword')
    token = data.get('turnstileToken')

    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("SELECT password FROM USERS WHERE user_id = :1", [user_id])
        row = cursor.fetchone()
        
        if not row or row[0] != current_pw:
            return jsonify({"error": "현재 비밀번호가 일치하지 않습니다."}), 400

        cursor.execute("UPDATE USERS SET password = :1 WHERE user_id = :2", [new_pw, user_id])
        conn.commit()
        return jsonify({"message": "비밀번호가 변경되었습니다."})

    except Exception as e:
        if 'conn' in locals(): conn.rollback()
        print(f"[Password Change Error] {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# 3. 기타 기능
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    try:
        msg = update_box_office_data()
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/recommend/weather', methods=['GET'])
def api_recommend_weather():
    try:
        condition = request.args.get('condition', 'Clear')
        tag_map = {'Clear': 'tag:Clear', 'Rain': 'tag:Rain', 'Snow': 'tag:Snow', 'Clouds': 'tag:Cloudy'}
        target_tag = tag_map.get(condition, 'tag:Clear')

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT t.track_title, t.preview_url, a.album_cover_url, m.title as movie_title
            FROM TRACKS t
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            JOIN ALBUMS a ON t.album_id = a.album_id
            LEFT JOIN MOVIE_OSTS mo ON t.track_id = mo.track_id
            LEFT JOIN MOVIES m ON mo.movie_id = m.movie_id
            WHERE tt.tag_id = :1
        """, [target_tag])
        
        results = []
        for row in cursor.fetchall():
            results.append({
                "title": row[0], "preview": row[1], "cover": row[2], "movie": row[3]
            })
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    # 로컬 테스트용 실행 (Docker에서는 CMD로 실행됨)
    app.run(debug=True, host='0.0.0.0', port=5000)