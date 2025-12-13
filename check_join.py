import oracledb
import config

def check_data_mismatch(target_tag):
    print(f"\n🔍 [데이터 불일치 추적] 태그 '{target_tag}'의 연결 상태를 확인합니다...")
    
    conn = None
    try:
        conn = oracledb.connect(
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            dsn=config.DB_DSN
        )
        cur = conn.cursor()

        # 1. 태그 테이블에 있는 Track ID들 가져오기
        print(f"\n1️⃣ TRACK_TAGS 테이블에서 '{target_tag}'가 달린 Track ID 목록:")
        cur.execute("SELECT track_id FROM TRACK_TAGS WHERE LOWER(tag_id) = LOWER(:tag)", [target_tag.strip()])
        tag_rows = cur.fetchall()
        
        if not tag_rows:
            print("   ❌ 태그 테이블에도 데이터가 없습니다. (아까 3개 있다면서요? 다시 확인 필요)")
            return

        track_ids_in_tag = [row[0] for row in tag_rows]
        print(f"   👉 발견된 ID ({len(track_ids_in_tag)}개): {track_ids_in_tag}")

        # 2. 각 Track ID가 실제로 TRACKS 테이블에 있는지 확인
        print("\n2️⃣ TRACKS 테이블 조회 결과:")
        found_count = 0
        for tid in track_ids_in_tag:
            cur.execute("SELECT track_title FROM TRACKS WHERE track_id = :id", [tid])
            track_row = cur.fetchone()
            
            if track_row:
                print(f"   ✅ ID '{tid}': 있음 -> 제목: {track_row[0]}")
                found_count += 1
            else:
                print(f"   ❌ ID '{tid}': 없음! (이게 원인입니다. 노래가 삭제됐거나 ID가 다름)")

        print("-" * 50)
        if found_count == 0:
            print("🚨 [결론] 태그는 달려있는데, 해당하는 노래가 TRACKS 테이블에 하나도 없습니다.")
            print("   👉 해결책: 해당 노래를 DB에 다시 추가하거나, 올바른 ID로 태그를 다시 달아야 합니다.")
        elif found_count < len(track_ids_in_tag):
            print("⚠️ [결론] 일부 노래만 DB에 존재합니다.")
        else:
            print("❓ [미스테리] 노래도 다 있는데 왜 JOIN이 안 될까요? (공백 문자 등 확인 필요)")

    except Exception as e:
        print(f"❌ 오류 발생: {e}")
    finally:
        if conn: conn.close()

if __name__ == "__main__":
    check_data_mismatch("tag:jpop")