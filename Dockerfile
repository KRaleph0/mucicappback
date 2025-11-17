# 1. 베이스 이미지 (파이썬 3.10 슬림 버전)
FROM python:3.10-slim

# 2. 작업 폴더 설정
WORKDIR /app

# 3. 오라클 클라이언트 설치 (파이썬이 오라클 DB에 접속하기 위해 필수)
RUN apt-get update && apt-get install -y libaio1 wget unzip \
 && wget https://download.oracle.com/otn_software/linux/instantclient/2112000/instantclient-basic-linux.x64-21.12.0.0.0dbru.zip \
 && unzip instantclient-basic-linux.x64-21.12.0.0.0dbru.zip \
 && sh -c "echo /app/instantclient_21_12 > /etc/ld.so.conf.d/oracle-instantclient.conf" \
 && ldconfig \
 && rm -f *.zip \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 4. 파이썬 라이브러리 설치 (requirements.txt 파일이 필요합니다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 나머지 코드 복사 (app.py 등)
COPY . .

# 6. 서버 실행 (예: Flask)
# (app.py 파일을 5000번 포트로 실행)
CMD ["flask", "--app", "app", "run", "--host=0.0.0.0", "--port=5000"]