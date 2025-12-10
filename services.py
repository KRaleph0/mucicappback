import requests
from datetime import datetime, timedelta
import config
import database
import utils

# [개선] 정확도 우선 검색 알고리즘
def find_best_track(titles, headers):
    """
    여러 제목(한글, 영문, 원제)으로 검색하여 가장 유사도가 높은 트랙 1개를 반환
    """
    candidates = []
    seen_titles = set()
    
    # 중복 제목 제거 후 검색 후보 리스트 생성
    for t in titles:
        if t and t not in seen_titles:
            candidates.append(t)
            seen_titles.add(t)
            
    best_match = None
    highest_score = 0.0

    for title in candidates:
        try:
            # 검색어에 'ost'를 붙여서 정확도 향상
            params = {
                "q": f"{title} ost", 
                "type": "track", 
                "limit": 5,  # 상위 5개 후보 분석
                "market": "KR"
            }
            res = requests.get(f"{config.SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
            
            tracks = res.get('tracks', {}).get('items', [])
            
            for track in tracks:
                # 1. 제목 유사도
                score_name = utils.get_similarity(title, track['name'])
                # 2. 앨범명 유사도 (OST 앨범인 경우 앨범명에 영화 제목이 들어감)
                score_album = utils.get_similarity(title, track['album']['name'])
                
                # 둘 중 더 높은 점수 채택
                final_score = max(score_name, score_album)
                
                # [핵심] 현재까지 찾은 것 중 점수가 더 높으면 갱신 (정확도 경쟁)
                if final_score > highest_score:
                    highest_score = final_score
                    best_match = track

        except Exception as e:
            print(f"[Search Error] {title}: {e}")
            continue

    # 최소 정확도 0.4 이상이어야 채택 (너무 엉뚱한 결과 방지)
    if highest_score >= 0.4:
        print(f"   -> Match Found: {best_match['name']} (Score: {highest_score:.2f})")
        return best_match
    
    return None

def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        # Spotify 상세 정보 조회
        t_res = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        
        # 오디오 특징 조회 (BPM, 분위기 등)
        a_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        # 데이터 추출
        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '') # 큰 이미지 사용
        bpm = a_data.get('tempo', 0)
        k_int = a_data.get('key', -1)
        dur = utils.ms_to_iso_duration(t_data.get('duration_ms', 0))
        mkey = config.PITCH_CLASS[k_int] if 0 <= k_int < 12 else 'Unknown'

        # 1. 앨범 정보 저장
        if aid:
            cursor.execute("""
                MERGE INTO ALBUMS USING dual ON (album_id=:1) 
                WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)
            """, [aid, img])
        
        # 2. 트랙 정보 저장 (Update or Insert)
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN 
                UPDATE SET t.bpm=:bpm, t.music_key=:mkey, t.duration=:dur, t.image_url=:img, t.preview_url=:prev
            WHEN NOT MATCHED THEN 
                INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
                VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur, 0)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 
              'img':img, 'bpm':bpm, 'mkey':mkey, 'dur':dur})

        # 3. 태그 생성 및 저장
        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        
        # 분위기 태그
        e = a_data.get('energy', 0)
        v = a_data.get('valence', 0)
        if e > 0.7: tags.add('tag:Exciting')
        if e < 0.4: tags.add('tag:Rest')
        if v < 0.3: tags.add('tag:Sentimental')
        if v > 0.7: tags.add('tag:Pop')
        
        # 장르 태그 매핑
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", 
                 "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", 
                 "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", 
                 "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        
        for g in genres:
            for k, val in g_map.items(): 
                if k in g: tags.add(val)
        
        for t in tags:
            try: 
                cursor.execute("""
                    MERGE INTO TRACK_TAGS USING dual ON (track_id=:1 AND tag_id=:2) 
                    WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)
                """, [track_id, t])
            except: pass
        
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
        
        # 어제 날짜 기준 박스오피스 조회
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params={
            "key": config.KOBIS_API_KEY, 
            "targetDt": target_dt, 
            "itemPerPage": "10"
        }).json()
        
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not movie_list: 
            return "KOBIS 데이터 없음"
            
        updated_count = 0
        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"[{rank}위] {title} 처리 중...")
            
            # 메타데이터 (장르, 영문제목 등)
            genres, title_en, title_og = utils.get_kobis_metadata(title)
            
            # 포스터 이미지 (TMDB)
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": config.TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'): 
                    poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass
            
            # DB: 영화 정보 저장
            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d 
                    ON (m.movie_id=d.mid) 
                    WHEN MATCHED THEN UPDATE SET rank=:rank, poster_url=:poster 
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)
                """, {'mid':title, 'title':title, 'rank':rank, 'poster':poster_url})
                conn.commit()
            except Exception as e:
                print(f"Movie DB Error: {e}")

            # DB: OST 검색 및 저장
            matched_track = find_best_track([title_og, title_en, title], headers)
            if matched_track:
                tid = matched_track['id']
                save_track_details(tid, cursor, headers, genres)
                try:
                    # 기존 매핑 삭제 후 재등록 (순위 변동 대응)
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':title, 'tid':tid})
                    conn.commit()
                    updated_count += 1
                except Exception as e:
                    print(f"Mapping DB Error: {e}")
        
        return f"업데이트 완료 ({updated_count}/10건 OST 매칭)"
    except Exception as e:
        return f"Batch Error: {e}"