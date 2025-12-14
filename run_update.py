from services import update_box_office_data
from database import init_db_pool

print("--- 수동 업데이트 시작 ---")

try:
    # 1. DB 연결 풀 초기화 (이게 없어서 에러가 났던 겁니다!)
    init_db_pool()
    
    # 2. 업데이트 실행
    result = update_box_office_data()
    
    print("--- 결과 ---")
    print(result)

except Exception as e:
    print(f"❌ 스크립트 실행 중 에러 발생: {e}")