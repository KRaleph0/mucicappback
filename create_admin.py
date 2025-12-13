import oracledb
import config
from werkzeug.security import generate_password_hash

def create_admin_user():
    print("👑 관리자 계정 생성/승격 도구")
    user_id = input("관리자로 만들 ID 입력: ")
    
    conn = oracledb.connect(
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        dsn=config.DB_DSN
    )
    cur = conn.cursor()
    
    try:
        # 1. 이미 존재하는지 확인
        cur.execute("SELECT user_id FROM USERS WHERE user_id=:1", [user_id])
        if cur.fetchone():
            # 이미 있으면 권한만 admin으로 수정
            cur.execute("UPDATE USERS SET role='admin' WHERE user_id=:1", [user_id])
            print(f"✅ 기존 유저 '{user_id}'를 관리자로 승격했습니다.")
        else:
            # 없으면 새로 생성
            pw = input("비밀번호 입력: ")
            nick = input("닉네임 입력: ")
            hashed_pw = generate_password_hash(pw)
            cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'admin')", [user_id, hashed_pw, nick])
            print(f"✅ 새로운 관리자 '{user_id}'를 생성했습니다.")
            
        conn.commit()
    except Exception as e:
        print(f"❌ 오류 발생: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    create_admin_user()