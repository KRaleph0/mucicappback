FROM python:3.10-slim

WORKDIR /app

# Oracle Instant Client 설치에 필요한 패키지 설치
RUN apt-get update \
    && apt-get install -y \
        wget \
        unzip \
        ca-certificates \
        libaio-dev \
        libssl-dev \
        libffi-dev \
        build-essential \
    && mkdir -p /opt/oracle \
    && wget https://download.oracle.com/otn_software/linux/instantclient/instantclient-basiclite-linuxx64.zip -O /opt/oracle/instantclient.zip \
    && unzip /opt/oracle/instantclient.zip -d /opt/oracle \
    && rm /opt/oracle/instantclient.zip \
    && echo "/opt/oracle/instantclient_23_5" > /etc/ld.so.conf.d/oracle-instantclient.conf \
    && ldconfig

# 파이썬 패키지 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 복사
COPY . .

CMD ["flask", "--app", "app", "run", "--host=0.0.0.0", "--port=5000"]
