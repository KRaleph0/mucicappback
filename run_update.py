from app import app
from services import update_box_office_data

print("--- 수동 업데이트 시작 ---")

# [핵심] Flask 앱 컨텍스트 안에서 실행해야 DB 연결(g, current_app)이 가능합니다.
with app.app_context():
    try:
        result = update_box_office_data()
        print("--- 결과 ---")
        print(result)
    except Exception as e:
        print(f"❌ 스크립트 실행 중 에러 발생: {e}")