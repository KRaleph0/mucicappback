import requests
from datetime import datetime, timedelta
import config
import database
import utils

# [개선] 정확도 우선 검색 & 로그 출력
def find_best_track(titles, headers):
    candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen: candidates.append(t); seen.add(t)
            
    best_match = None
    highest_score = 0.0

    print(f"   Searching for: {candidates}") # 로그 추가

    for title in candidates:
        try:
            params = {"q": f"{title} ost", "type": "track", "limit": 5, "market": "KR"}
            res = requests.get(f"{config.SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
            tracks = res.get('tracks', {}).get('items', [])
            
            if not tracks:
                print(f"   -> No results for '{title}'") # 로그 추가

            for track in tracks:
                score_name = utils.get_similarity(title, track['name'])
                score_album = utils.get_similarity(title, track['album']['name'])
                final_score = max(score_name, score_album)
                
                if final_score > highest_score:
                    highest_score = final_score
                    best_match = track
        except Exception as e: 
            print(f"   -> Error searching '{title}': {e}") # 에러 로그

    if highest_score >= 0.4:
        print(f"   -> Match Found! {best_match['name']} (Score: {highest_score:.2f})")
        return best_match
    
    print("   -> No suitable match found.")
    return None

def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        t_res = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        
        # (오디오 특징 API 호출 - 실패해도 진행하도록 수정)
        a_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        # 데이터 추출
        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        bpm = a_data.get('tempo', 0)
        
        # DB 저장 (MERGE)
        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.image_url=:img, t.preview_url=:prev
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, views)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, 0)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm})

        # 태그 저장 (생략 - 기존 로직 사용)
        cursor.connection.commit()
        return t_data
    except Exception as e: 
        print(f"[Save Error] {e}")
        return None

def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        headers = utils.get_spotify_headers()
        
        # 어제 날짜
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params={"key": config.KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not movie_list: return "KOBIS 데이터 없음 (API 키 확인 필요)"
        
        count = 0
        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"Processing [{rank}위]: {title}")
            
            # 영화 정보 저장
            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d 
                    ON (m.movie_id=d.mid) 
                    WHEN MATCHED THEN UPDATE SET rank=:rank 
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank) VALUES (:mid, :title, :rank)
                """, {'mid':title, 'title':title, 'rank':rank})
                conn.commit()
            except Exception as e: print(f"Movie DB Error: {e}")

            # OST 매칭 및 저장
            genres, title_en, title_og = utils.get_kobis_metadata(title)
            matched_track = find_best_track([title_og, title_en, title], headers)
            
            if matched_track:
                tid = matched_track['id']
                save_track_details(tid, cursor, headers, genres)
                try:
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':title, 'tid':tid})
                    conn.commit()
                    count += 1
                except: pass
        
        return f"업데이트 완료 ({count}/10건 매칭)"
    except Exception as e: 
        print(f"[Batch Critical Error] {e}")
        return f"Error: {e}"