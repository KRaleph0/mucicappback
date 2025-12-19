from rdflib import Graph, Namespace, RDF, SKOS, Literal

class SkosManager:
    def __init__(self, file_path):
        self.g = Graph()
        try:
            # 파일 경로 문제 방지를 위해 절대 경로 사용 권장이나, 현재 구조상 상대 경로 유지
            self.g.parse(file_path, format="turtle")
            print(f"✅ [SKOS] '{file_path}' 로드 성공! (트리플 수: {len(self.g)})")
        except Exception as e:
            print(f"❌ [SKOS] 로드 실패: {e}")

        self.KOMC = Namespace("https://knowledgemap.kr/komc/def/")
    
    def _normalize(self, text):
        """대소문자 무시 및 공백 제거"""
        return text.replace("tag:", "").strip().lower()

    def _find_concept_uri(self, keyword):
        """키워드(ID 또는 라벨)로 개념 URI 찾기"""
        target = self._normalize(keyword)
        candidates = []
        
        # 1. URI의 끝부분(ID)으로 검색 (예: 'Pop' -> komc:Genre_Pop, komc:tag_Pop)
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            if str(s).split("_")[-1].lower() == target:
                candidates.append(s)
        
        # 2. ID 매칭이 없을 경우 라벨(prefLabel)로 검색 (예: '휴식' -> komc:tag_Rest)
        if not candidates:
            for s, p, o in self.g.triples((None, SKOS.prefLabel, None)):
                if str(o).lower() == target:
                    candidates.append(s)
        
        if not candidates:
            return None

        # [버그 수정 핵심] 
        # 후보군 중에 'Genre_'나 'Weather_'로 시작하는(계층 구조를 가진) 개념이 있다면 그것을 우선 선택합니다.
        # 이렇게 해야 'Pop' 검색 시 'tag_Pop'(말단) 대신 'Genre_Pop'(상위)이 선택되어 하위 장르(JPop 등)까지 검색됩니다.
        for uri in candidates:
            uri_str = str(uri)
            if "Genre_" in uri_str or "Weather_" in uri_str:
                return uri

        # 계층 구조 개념이 없으면 첫 번째 후보 반환
        return candidates[0]

    def _get_all_labels(self, uri):
        """특정 개념의 ID와 모든 라벨(한/영)을 반환"""
        labels = set()
        # ID 추가 (예: JPop)
        labels.add(str(uri).split("_")[-1])
        # 라벨 추가 (예: J-Pop, Jpop)
        for lbl in self.g.objects(uri, SKOS.prefLabel):
            labels.add(str(lbl))
        return labels

    def get_broader_tags(self, tag):
        """상위 개념 찾기 (저장용)"""
        uri = self._find_concept_uri(tag)
        if not uri: return set()
        
        broader_tags = set()
        # 상위(broader) 개념의 ID 가져오기
        for parent in self.g.objects(uri, SKOS.broader):
            broader_tags.add(str(parent).split("_")[-1])
        
        # [중요] 연관(related) 개념도 상위 개념처럼 저장에 활용 (Genre_JPop -> tag_Jpop)
        for rel in self.g.objects(uri, SKOS.related):
            broader_tags.add(str(rel).split("_")[-1])
            
        return broader_tags

    def get_narrower_tags(self, tag):
        """하위 개념 및 동의어 찾기 (검색용)"""
        root = self._find_concept_uri(tag)
        # 기본적으로 검색어 자체 포함
        expanded_tags = {self._normalize(tag)}
        
        if not root: return list(expanded_tags)

        # 1. 루트 개념의 모든 라벨 추가 (Pop, 팝)
        expanded_tags.update(self._get_all_labels(root))

        # 2. 하위 개념 재귀 탐색
        def traverse(node):
            # narrower 탐색
            for child in self.g.objects(node, SKOS.narrower):
                expanded_tags.update(self._get_all_labels(child))
                # 하위 개념의 연관 태그도 추가 (Genre_JPop -> tag_Jpop)
                for rel in self.g.objects(child, SKOS.related):
                    expanded_tags.update(self._get_all_labels(rel))
                traverse(child)
                
            # (추가) 현재 노드의 related 태그도 검색 범위에 포함
            for rel in self.g.objects(node, SKOS.related):
                expanded_tags.update(self._get_all_labels(rel))

        traverse(root)
        
        # 3. 소문자 처리 안된 원본들도 있을 수 있으므로 리스트로 반환
        return list(expanded_tags)
    
    def get_weather_tags(self, weather_keyword):
        # (기존 로직 유지)
        uri = self.KOMC[f"Weather_{weather_keyword}"]
        if (uri, RDF.type, SKOS.Concept) not in self.g: uri = self.KOMC["Weather_Default"]
        tags = []
        for rel in self.g.objects(uri, SKOS.related):
            for lbl in self.g.objects(rel, SKOS.prefLabel):
                if lbl.language == 'ko': tags.append(str(lbl))
        return tags