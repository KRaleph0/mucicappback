import os
import oracledb
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# 모듈 import
from config import UPLOAD_FOLDER
from database import get_db_connection, close_db
from services import update_box_office_data
from utils import allowed_file, verify_turnstile  # verify_turnstile 추가됨

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 파일 크기 제한 (16MB)

# 업로드 폴더 자동 생성
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

CORS(app)

# DB 연결 해제 핸들러 등록
app.teardown_appcontext(close_db)

# =========================================================
# 1. 인증 (Auth) API: 회원가입, 로그인
# =========================================================

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    """회원가입 (캡차 검증 포함)"""
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    nickname = data.get('nickname')
    token = data.get('turnstileToken')

    # 1. 캡차 검증
    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    # 2. 필수 값 확인
    if not all([user_id, password, nickname]):
        return jsonify({"error": "모든 필드를 입력해주세요."}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 3. 아이디 중복 확인
        cursor.execute("SELECT user_id FROM USERS WHERE user_id = :1", [user_id])
        if cursor.fetchone():
            return jsonify({"error": "이미 존재하는 아이디입니다."}), 409

        # 4. 회원 정보 저장
        cursor.execute(
            "INSERT INTO USERS (user_id, password, nickname, profile_img) VALUES (:1, :2, :3, :4)",
            [user_id, password, nickname, None] 
        )
        conn.commit()
        return jsonify({"message": "회원가입 성공"}), 201

    except Exception as e:
        conn.rollback()
        print(f"[Signup Error] {e}")
        return jsonify({"error": "회원가입 처리 중 오류 발생"}), 500


@app.route('/api/auth/login', methods=['POST'])
def login():
    """로그인 (캡차 검증 포함)"""
    data = request.json
    user_id = data.get('user_id')
    password = data.get('password')
    token = data.get('turnstileToken')

    # 1. 캡차 검증
    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 2. 사용자 확인
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
        return jsonify({"error": "로그인 처리 중 오류 발생"}), 500


# =========================================================
# 2. 사용자 (User) API: 프로필, 비밀번호 변경
# =========================================================

@app.route('/api/user/profile', methods=['GET', 'POST'])
def handle_profile():
    conn = get_db_connection()
    cursor = conn.cursor()

    # [GET] 프로필 조회
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

    # [POST] 프로필 수정 (닉네임, 이미지)
    if request.method == 'POST':
        try:
            user_id = request.form.get('user_id')
            nickname = request.form.get('nickname')
            file = request.files.get('profileImage')

            # 1. 닉네임 업데이트
            if nickname:
                cursor.execute("UPDATE USERS SET nickname = :1 WHERE user_id = :2", [nickname, user_id])

            # 2. 이미지 업로드
            web_path = None
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(file_path)
                
                web_path = f"/uploads/{filename}"
                cursor.execute("UPDATE USERS SET profile_img = :1 WHERE user_id = :2", [web_path, user_id])

            conn.commit()
            
            # 변경된 최신 이미지 경로 반환
            return jsonify({
                "message": "프로필 저장 완료", 
                "image_url": web_path
            })

        except Exception as e:
            conn.rollback()
            print(f"[Profile POST Error] {e}")
            return jsonify({"error": "저장 중 오류 발생"}), 500


@app.route('/api/user/password', methods=['POST'])
def update_password():
    """비밀번호 변경 (캡차 검증 포함)"""
    data = request.json
    user_id = data.get('user_id')
    current_pw = data.get('currentPassword')
    new_pw = data.get('newPassword')
    token = data.get('turnstileToken')

    # 1. 캡차 검증
    is_valid, err_msg = verify_turnstile(token)
    if not is_valid:
        return jsonify({"error": err_msg}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        # 2. 현재 비밀번호 확인
        cursor.execute("SELECT password FROM USERS WHERE user_id = :1", [user_id])
        row = cursor.fetchone()
        
        if not row or row[0] != current_pw:
            return jsonify({"error": "현재 비밀번호가 일치하지 않습니다."}), 400

        # 3. 새 비밀번호 변경
        cursor.execute("UPDATE USERS SET password = :1 WHERE user_id = :2", [new_pw, user_id])
        conn.commit()
        
        return jsonify({"message": "비밀번호가 변경되었습니다."})

    except Exception as e:
        conn.rollback()
        print(f"[Password Change Error] {e}")
        return jsonify({"error": "비밀번호 변경 중 오류 발생"}), 500


# =========================================================
# 3. 기존 관리자 및 추천 기능
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    """(관리자용) 박스오피스 강제 업데이트"""
    try:
        msg = update_box_office_data()
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/recommend/weather', methods=['GET'])
def api_recommend_weather():
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

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)