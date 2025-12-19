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
        
        # 1. URI의 끝부분(ID)으로 검색 (예: 'Pop' -> komc:Genre_Pop)
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            if str(s).split("_")[-1].lower() == target:
                return s
        
        # 2. 라벨(prefLabel)로 검색 (예: '휴식' -> komc:tag_Rest)
        for s, p, o in self.g.triples((None, SKOS.prefLabel, None)):
            if str(o).lower() == target:
                return s
                
        return None

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