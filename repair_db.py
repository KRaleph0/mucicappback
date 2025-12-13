import oracledb
import config
from services import save_track_details
from utils import get_spotify_headers

def repair_missing_tracks():
    print("\n🚑 [데이터 복구 모드] 유실된 트랙 정보를 복구합니다...")
    
    conn = None
    try:
        # DB 연결
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()
        
        # 1. 유령 ID 찾기 (태그는 있는데 TRACKS에 없는 놈들)
        print("\n1️⃣ 손상된 데이터 스캔 중...")
        cur.execute("""
            SELECT DISTINCT tt.track_id 
            FROM TRACK_TAGS tt 
            LEFT JOIN TRACKS t ON tt.track_id = t.track_id 
            WHERE t.track_id IS NULL
        """)
        
        missing_ids = [row[0] for row in cur.fetchall()]
        
        if not missing_ids:
            print("   ✅ 다행입니다! 유실된 데이터가 없습니다. DB는 건강합니다.")
            return

        print(f"   ⚠️ 발견됨! 총 {len(missing_ids)}개의 노래가 내용이 지워져 있습니다.")
        print(f"   👉 목록: {missing_ids}")
        
        # 2. Spotify에서 정보 받아와서 되살리기
        print("\n2️⃣ Spotify API로 정보 복구 및 DB 삽입 시작...")
        headers = get_spotify_headers() # 토큰 발급
        
        success_count = 0
        for tid in missing_ids:
            try:
                print(f"   🔨 복구 시도: {tid} ... ", end='')
                # [핵심] 서비스 로직을 재사용하여 트랙 정보 저장
                # (save_track_details 함수가 Spotify 조회 + DB 저장을 다 해줍니다)
                result = save_track_details(tid, cur, headers, [])
                
                if result:
                    conn.commit() # 저장 확정
                    print(f"✅ 성공! (제목: {result['name']})")
                    success_count += 1
                else:
                    print("❌ 실패 (Spotify에도 없는 ID인가요?)")
                    
            except Exception as e:
                print(f"❌ 에러: {e}")

        print("-" * 50)
        print(f"🎉 복구 완료! 총 {success_count}/{len(missing_ids)}개 트랙을 되살렸습니다.")
        print("👉 이제 웹사이트에서 다시 검색해보세요!")

    except Exception as e:
        print(f"\n❌ [치명적 오류] 복구 스크립트 실행 중 문제 발생: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    repair_missing_tracks()