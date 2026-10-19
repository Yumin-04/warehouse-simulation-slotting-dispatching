from pathlib import Path
import simpy
import pandas as pd 
import numpy as np
import random
import math                                         # [수정사항] 유클리드 대각선 거리를 계산하기 위해 math 모듈 추가
from itertools import combinations  # 주문 내에서 함께 출고되는 부품쌍 생성 (Kitting Slotting에 사용)
from collections import Counter     # 함께 출고되는 부품쌍의 출현 빈도 계산 (어떤 부품을 서로 가깝게 배치할지 판단하기 위함)
import matplotlib.pyplot as plt     # 시각화 및 스타일링
import seaborn as sns               # 히트맵용 시각화
import matplotlib.patches as patches # 2D 궤적 맵에서 랙(Rack)을 그리기 위함

# 한글 폰트 설정
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False

# --- 시스템 환경 설정 (상수 정의) ---
# 창고의 물리적 레이아웃과 물류 로봇 성능 정의
RACK_START_Y, RACK_END_Y = 4.0, 15.1       # 랙이 설치된 Y축의 시작과 끝 좌표 범위 (4.0m ~ 15.1m 사이에만 적재 가능)

# -------------------------------------------------------------------------
# [레이아웃 확장 수정사항] 기존 1개 블록(통로 6개) 레이아웃을 그대로 복제하여
# 옆으로 3개를 나란히 붙인 구조로 확장. 통로 총 개수 6개 -> 18개.
# 블록끼리는 완전히 맞닿아 있는 하나의 연속된 창고로 취급(BLOCK_GAP=0)하여,
# 기존의 "교차 통로를 거쳐 이동" 로직(get_estimated_travel_time 등)이
# 별도 수정 없이 그대로 확장 적용되도록 함(모두 X좌표 기반 계산이라 블록 수만 늘려도 호환됨).
# -------------------------------------------------------------------------
BASE_AISLE_X = [1.2, 3.6, 6.0, 8.4, 10.8, 13.2] # 블록 1개 안에서의 통로 X좌표 (기존 레이아웃 그대로)
BLOCK_WIDTH = 14.4   # 블록 1개의 전체 폭 (랙+통로 포함, 기존 rack_spans 총합과 동일)
BLOCK_GAP = 0.0      # [수정사항] 블록 사이 여유 공간을 0으로 설정 -> 경계의 단면 랙(0.6m) 두 개가 맞닿아
                     # 내부 랙과 동일한 1.2m 폭이 되면서 이음매 없이 완전히 붙어있는 하나의 연속된 레이아웃이 됨
NUM_BLOCKS = 3       # 옆으로 붙일 블록 개수 (1개 -> 3개로 확장)

AISLE_X = [] # 3개 블록의 통로 X좌표를 모두 합친 최종 통로 리스트 (총 6*3 = 18개, 2.4m 간격으로 연속)
for _b in range(NUM_BLOCKS):
    _offset = _b * (BLOCK_WIDTH + BLOCK_GAP) # 블록 순서에 따라 오른쪽으로 밀어낼 X 오프셋
    AISLE_X.extend([round(x + _offset, 2) for x in BASE_AISLE_X])

# [수정사항] 기존처럼 통로에도 순서대로 번호(A01~A18)를 부여. 블록마다 번호를 반복하지 않고
# 왼쪽부터 오른쪽까지 창고 전체를 관통하는 고유 번호로 매김 (실제로는 하나로 이어진 창고이므로)
AISLE_LABELS = {x: f'A{i+1:02d}' for i, x in enumerate(AISLE_X)}

# 작업 스테이션 위치 (x, y, z)
# -------------------------------------------------------------------------
# [수정사항 - 스테이션 위치를 CTU 경로에 맞게 재배치]
# 스테이션은 "블록과 블록 사이 경계"에 위치해야 앞뒤 블록에서 동시에
# 접근하기 좋습니다. 블록이 NUM_BLOCKS개면 블록 사이 경계는
# (NUM_BLOCKS - 1)개가 생기고, 이 경계마다 스테이션을 하나씩 자동
# 배치합니다. 3블록 구조에서는 경계가 2군데(블록1/2 사이 X=14.4,
# 블록2/3 사이 X=28.8)이므로 정확히 스테이션 2개(S01, S02)가 생성됩니다.
# 블록 개수가 늘어나면 경계 개수(=스테이션 개수)도 자동으로 늘어납니다.
# -------------------------------------------------------------------------
STATIONS = {}
for _b in range(1, NUM_BLOCKS):
    _station_x = round(_b * (BLOCK_WIDTH + BLOCK_GAP), 2) # 블록 b와 블록 b+1 사이의 경계 X좌표
    STATIONS[f'S{_b:02d}'] = [_station_x, 0.5, 0]

CTU_SPEED_XY = 1.8  # 로봇의 XY 이동 속도 (m/s)
CTU_SPEED_Z = 0.5   # 로봇의 리프트(Z) 상하 이동 속도 (m/s)
TOTE_CAPACITY = 4   # [수정사항] CTU가 한 번에 운반할 수 있는 최대 토트 박스 수 (기존 7개 -> 4개로 하향 조정)
SHELF_HEIGHTS = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0] # 선반의 층별 높이(Z축) 리스트 (부품의 Z좌표로 사용)

# 교차 통로(Cross Aisle) 설정
# 로봇이 다른 통로로 넘어갈 때 랙을 관통하지 않고 앞쪽으로 빠져나와서 이동하는 기준 Y 좌표
CROSS_AISLE_Y = 3.0

# [추가] 물류기기(CTU) 운영 대수 상수화
# -------------------------------------------------------------------------
# CTU 대수는 스테이션 개수(len(STATIONS))에 맞춰 자동으로 결정됩니다.
# CTU 초기 위치도 STATIONS 딕셔너리를 순서대로 순회하며 자동 배정되므로,
# NUM_BLOCKS 값이 바뀌어도 별도 수정 없이 대응됩니다.
# -------------------------------------------------------------------------
NUM_CTUS = len(STATIONS)

# [버그 수정] 스테이션이 두 통로의 정중앙(예: A06=13.2와 A07=15.6의 정중앙인 X=14.4)에
# 위치하는 경우, 수학적으로는 두 통로까지의 거리가 완전히 동일(1.2m)해야 하는데
# 부동소수점 연산 오차(예: 1.200000000000001 vs 1.1999999999999993) 때문에
# 어느 쪽으로 스냅될지가 미세한 오차 방향에 따라 제멋대로 결정되는 문제가 있었습니다.
# 이로 인해 S01은 오른쪽 통로로, S02는 왼쪽 통로로 서로 다르게 스냅되어
# 스테이션 위치와 CTU 경로가 비대칭적으로 어긋나 보였습니다.
# round()로 소수점 6자리까지만 비교하여 부동소수점 노이즈를 제거하고,
# 정확히 동점일 경우 항상 리스트상 먼저 나오는(=더 작은, 왼쪽) 통로를 일관되게 선택하도록 고정했습니다.
def get_aisle_x(target_x):
    return min(AISLE_X, key=lambda a: round(abs(a - target_x), 6))

# 스테이션이 2개뿐이라 "X <= 6.0이면 S01, 아니면 S02"처럼 하드코딩된 임계값으로도 원래는 충분했지만,
# 나중에 스테이션 구성을 바꿀 때도 코드를 다시 손대지 않도록 가장 가까운 스테이션을 동적으로 찾는 범용 헬퍼로 통일
def get_nearest_station(x):
    return min(STATIONS.values(), key=lambda s: abs(s[0] - x))

# [추가 수정사항] CTU가 스테이션을 오갈 때는 이웃 통로로 우회하지 않고 스테이션의
# 정확한 중앙 좌표로 곧장 진입/진출해야 합니다. 그런데 get_aisle_x()를 스테이션
# 좌표에도 그대로 적용하면, 스테이션이 두 통로 정중앙에 있는 경우 엉뚱한 이웃
# 통로로 스냅되어버립니다(바로 위에서 고친 비대칭 문제와 같은 원인).
# 이를 근본적으로 해결하기 위해, 좌표가 스테이션의 X값과 정확히 일치하면
# 통로 스냅을 아예 건너뛰고 스테이션 좌표를 그대로 사용하는 헬퍼를 추가했습니다.
# (부품/랙 위치처럼 스테이션이 아닌 좌표는 기존처럼 가장 가까운 통로로 정상 스냅됩니다.)
STATION_XS = set(round(s[0], 6) for s in STATIONS.values())

def resolve_x(x_raw):
    if round(x_raw, 6) in STATION_XS: # 스테이션 좌표 자신이라면
        return x_raw                  # 스냅하지 않고 정확한 중앙값 그대로 사용
    return get_aisle_x(x_raw)         # 그 외(부품/랙 위치)는 기존처럼 통로 중심으로 스냅

# [추가 수정사항] 스테이션 ↔ 통로 이동은 '복귀'일 때만 대각선 단축을 쓰고
# '출발'일 때는 스테이션 X좌표에서 일단 수직으로 교차 통로까지 올라간 뒤 가로로
# 이동하는 방식이었는데, 이 때문에 스테이션 정중앙에 불필요한 수직선이 그려졌습니다.
# 스테이션 앞(Y<3.0) 공간은 복귀 때와 마찬가지로 장애물이 없는 개방 구역이므로,
# 출발이든 복귀든 방향에 관계없이 '스테이션 - 교차통로 진입점' 구간은 대각선으로,
# '교차통로 - 통로 안쪽' 구간만 직선으로 계산하도록 통일했습니다.
def compute_xy_dist(x1, y1, x2, y2):
    x1_is_station = round(x1, 6) in STATION_XS
    x2_is_station = round(x2, 6) in STATION_XS
    
    if x1_is_station and x2_is_station: # 스테이션 to 스테이션 (사실상 발생하지 않지만 안전하게 처리)
        return math.sqrt((x1 - x2)**2 + (y1 - y2)**2)
    elif x1_is_station: # 출발이 스테이션인 경우: 스테이션에서 목표 통로 진입점(X2, 교차통로)까지 대각선 + 통로 안쪽까지 직진
        diag = math.sqrt((x1 - x2)**2 + (CROSS_AISLE_Y - y1)**2)
        vert = abs(y2 - CROSS_AISLE_Y)
        return diag + vert
    elif x2_is_station: # 도착이 스테이션인 경우(복귀): 통로에서 교차통로까지 직진 + 스테이션까지 대각선
        vert = abs(y1 - CROSS_AISLE_Y)
        diag = math.sqrt((x1 - x2)**2 + (CROSS_AISLE_Y - y2)**2)
        return vert + diag
    else: # 둘 다 일반 통로(랙) 위치인 경우: 기존 로직 그대로
        if x1 == x2:
            return abs(y1 - y2)
        else:
            return abs(y1 - CROSS_AISLE_Y) + abs(x1 - x2) + abs(y2 - CROSS_AISLE_Y)

# ==========================================
# 창고 레이아웃 기반 예상 소요 시간 산출 함수 (기존 거리 함수 대체)
# ==========================================
def get_estimated_travel_time(pos1, pos2):
    """
    물류 로봇이 랙(Rack)을 뚫고 지나갈 수 없다는 물리적 제약을 반영하여
    두 위치(pos1, pos2) 사이의 '예상 이동 소요 시간'을 계산하는 함수.
    디스패칭 시 어떤 작업이 가장 '빨리' 도달할 수 있는지 판단하는 기준이 됨.
    """
    x1_raw, y1, z1 = pos1 # 시작 지점의 x, y, z 좌표를 언패킹
    x2_raw, y2, z2 = pos2 # 도착 지점의 x, y, z 좌표를 언패킹
    
    # [수정사항] 부품 좌표는 통로 중심으로 보정하되, 스테이션 좌표는 그대로 유지 (resolve_x)
    x1 = resolve_x(x1_raw)
    x2 = resolve_x(x2_raw)
    
    xy_dist = compute_xy_dist(x1, y1, x2, y2) # [수정사항] 스테이션 관여 여부에 따라 대각선/직선을 자동으로 구분해 계산
        
    dz = abs(z1 - z2) # Z축(높이) 차이 계산
    
    # 평면 이동 속도와 리프트 속도 중 병목이 되는(더 오래 걸리는) 진짜 소요 시간을 반환
    return max(xy_dist / CTU_SPEED_XY, dz / CTU_SPEED_Z) 

# ==========================================
# CTU class 물류 로봇
# ==========================================
class CTU: # 물류 로봇(CTU) 객체를 정의하는 클래스
    def __init__(self, env, id, warehouse, dispatch_rule): # 로봇 초기화 메서드 (환경, ID, 소속 창고, 디스패칭 룰을 인자로 받음)
        self.env = env # SimPy 시뮬레이션 환경 객체를 로봇의 속성으로 저장
        self.id = id # CTU를 구별하기 위한 고유 번호
        self.warehouse = warehouse # 로봇이 작업할 창고 객체의 참조를 저장
        self.dispatch_rule = dispatch_rule # 현재 로봇에 적용할 작업 할당 방식(Nearest, Density 등) 저장
        
        # -------------------------------------------------------------------------
        # [수정사항] 스테이션 개수(=CTU 대수)에 맞춰 각 CTU가 서로 다른 스테이션에서
        # 출발하도록 일반화. 기존에는 "id==0이면 S01, 아니면 S02"로 스테이션이
        # 딱 2개일 때만 맞는 하드코딩이었으나, 이제는 STATIONS 딕셔너리 순서대로
        # id번째 스테이션을 자동 배정합니다.
        # -------------------------------------------------------------------------
        station_list = list(STATIONS.values())
        self.pos = station_list[id % len(station_list)] # 로봇 ID에 대응하는 스테이션 좌표를 초기 위치로 설정

        # 총 이동거리 및 작업 수 기록
        self.total_distance = 0 # 로봇이 이동한 총 누적 거리를 0으로 초기화
        self.completed_jobs = 0 # 로봇이 완료한 총 피킹 작업 수를 0으로 초기화
        self.busy = False # 로봇의 현재 상태를 대기 중(False)으로 초기화
        
        # 이 CTU가 현재 작업하기 위해 점유 중인 통로(X좌표)들을 담아두는 집합(Set) 변수 추가
        self.current_batch_aisles = set() 
        
        self.process = env.process(self.run()) # 로봇의 메인 작업 루프인 run()을 SimPy 이벤트 큐에 프로세스로 등록

    # 이동 시간 및 거리 계산 (랙 관통 방지를 위한 교차 통로 경유 + 스테이션 구간 대각선 단축)
    def calculate_travel_time(self, target_pos, is_return=False): # is_return은 더 이상 분기에 쓰이지 않지만(자동 감지로 대체) 호출부 호환을 위해 유지
        x1_raw, y1, z1 = self.pos # 로봇의 현재 위치 좌표 언패킹
        x2_raw, y2, z2 = target_pos # 로봇이 이동해야 할 목표 위치 좌표 언패킹
        
        # [수정사항] 부품/랙 위치는 통로 중앙 좌표로 보정하되, 스테이션 좌표는 그대로 유지 (resolve_x)
        x1 = resolve_x(x1_raw)
        x2 = resolve_x(x2_raw)
        
        # [수정사항] 출발/복귀 여부를 수동으로 나누지 않고, 스테이션이 관여하는 쪽을 자동 감지해 대각선 단축을 적용
        xy_dist = compute_xy_dist(x1, y1, x2, y2)
            
        dz = abs(z1 - z2) # Z축(상하 리프트) 이동 거리 계산
        
        travel_time = max(xy_dist / CTU_SPEED_XY, dz / CTU_SPEED_Z) # 평면 주행과 리프트 승강은 동시 수행되므로 둘 중 더 오래 걸리는 시간을 소요 시간으로 산정
        travel_distance = xy_dist + dz # 물리적인 총 모터 이동 거리(주행 거리 + 리프트 이동 거리) 합산
        
        return travel_time, travel_distance # 계산된 이동 시간과 총 이동 거리를 튜플로 반환

    # CTU의 실제 작업 수행 프로세스
    def run(self): # 시간 흐름에 따라 CTU가 지속적으로 수행할 제너레이터 메서드
        while True: # 시뮬레이션 종료 시까지 무한 반복
            if self.warehouse.job_queue: # 창고에 처리 대기 중인 주문 작업이 남아있는지 확인
                # 무조건 busy를 True로 바꾸지 않고, 겹치지 않는 배치를 받아올 수 일 때만 상태를 변경하도록 로직 순서 변경
                batch = self.warehouse.apply_dispatching(self, self.dispatch_rule) 
                
                if batch: # 겹치지 않는 할당받은 작업 묶음이 존재한다면
                    self.busy = True # 비로소 작업 상태(True)로 전환
                    
                    # 할당받은 작업 배치에 포함된 모든 통로의 X좌표를 추출하여 현재 CTU의 점유 통로로 등록
                    # (이후 다른 CTU가 디스패칭을 수행할 때 이 통로의 작업들은 투명 인간처럼 무시됨)
                    # [새로운 수정사항] 통로 점유 시 부품 좌표(예: 0.3)가 아닌 해당 통로의 중심 좌표(예: 1.2)로 등록하여 점유 충돌 완벽 방지
                    self.current_batch_aisles = set(get_aisle_x(job['pos'][0]) for job in batch)
                    
                    # [수정사항] 거리 기준 정렬에서 '예상 소요 시간' 기준 정렬로 변경
                    # (기존 코드) batch.sort(key=lambda j: get_estimated_travel_time(self.pos, j['pos'])) 
                    
                    # -------------------------------------------------------------------------
                    # [추가/수정] 배치 내 그리디(Greedy) 라우팅 적용 (Nearest Neighbor TSP)
                    # 기존에는 '초기 로봇 위치' 기준으로만 7개의 순서를 확정하여 널뛰기 동선이 발생했습니다.
                    # 이를 방지하고자, '가장 최근에 방문한 위치'를 기준으로 남은 작업들을 동적으로 재정렬합니다.
                    # -------------------------------------------------------------------------
                    ordered_batch = []
                    current_route_pos = self.pos # 라우팅 기준점을 현재 로봇 위치로 초기화
                    unvisited = batch.copy() # 아직 방문하지 않은 작업 목록 복사
                    
                    while unvisited:
                        # 현재 위치에서 예상 소요 시간이 가장 짧은 순으로 남은 작업들 정렬
                        unvisited.sort(key=lambda j: get_estimated_travel_time(current_route_pos, j['pos']))
                        next_job = unvisited.pop(0) # 가장 가까운 작업 추출
                        ordered_batch.append(next_job) # 실제 방문할 순서 리스트에 추가
                        current_route_pos = next_job['pos'] # 다음 계산을 위해 기준 위치를 갱신
                        
                    batch = ordered_batch # 최적화된 동선으로 기존 배치 덮어쓰기
                    # -------------------------------------------------------------------------
                    
                    for job in batch: # 할당받은 각 작업(부품 피킹)을 순회
                        # [새로운 수정사항] 부품(job['pos'])의 좌표는 랙 내부(예: 0.3)지만, 로봇 본체가 실제로 이동/정차해야 할 곳은 통로 중앙(1.2)
                        target_aisle_x = get_aisle_x(job['pos'][0])
                        drive_target_pos = (target_aisle_x, job['pos'][1], job['pos'][2]) # 로봇 본체의 최종 주행 목표 좌표 생성
                        
                        t, d = self.calculate_travel_time(drive_target_pos) # 해당 목표 통로 좌표까지의 주행 시간(t)과 거리(d) 계산
                        
                        self.warehouse.move_log.append({ # 분석을 위해 로봇의 이동 상세 이력을 창고의 로그 리스트에 추가
                            'time': self.env.now, # 현재 시뮬레이션 시간 기록
                            'ctu_id': self.id, # 어떤 로봇이 이동했는지 ID 기록
                            'from_x': self.pos[0], 'from_y': self.pos[1], 'from_z': self.pos[2], # 출발 지점 X, Y, Z 좌표 기록
                            'to_x': drive_target_pos[0], 'to_y': drive_target_pos[1], 'to_z': drive_target_pos[2], # 도착 주행 지점 X, Y, Z 좌표 기록
                            'item_id': job['item_id'], # 피킹한 대상 부품 코드 기록
                            'travel_time': t, 'travel_distance': d, # 해당 구간 이동 소요 시간과 이동 거리 기록
                            'dispatch_rule': self.dispatch_rule, # 당시 적용된 디스패칭 룰 기록
                        })
                        
                        # [새로운 수정사항] 주행 이동 시간(t) + CTU 로봇 팔(Fork)이 제자리에서 뻗어서 토트를 꺼내오는 순수 피킹 시간(예: 2초) 합산 반영
                        picking_time = 2.0
                        yield self.env.timeout(t + picking_time) # 계산된 주행 시간과 피킹 시간만큼 시뮬레이션 시간을 흘려보냄 
                        
                        self.pos = drive_target_pos # 로봇의 현재 위치를 방금 도착한 통로 중앙 좌표로 갱신 (부품 좌표가 아닙니다!)
                        self.total_distance += d # 로봇의 누적 이동 거리에 방금 이동한 거리를 더함
                        self.completed_jobs += 1 # 완료한 피킹 작업 개수 1 증가
                        
                    # 마지막 피킹 위치의 X좌표에 따라 복귀할 지정 스테이션 결정
                    last_job_x = self.pos[0] # 배치의 모든 피킹이 끝난 후 로봇의 현재(마지막) 통로 X 좌표 확인
                    target_station = get_nearest_station(last_job_x) # [레이아웃 확장 수정사항] 스테이션 개수와 무관하게 마지막 위치에서 가장 가까운 곳을 동적으로 복귀 지점으로 결정
                    
                    # 복귀할 때는 is_return=True를 넘겨주어 Y=3.0 앞쪽에서는 유클리드 거리로 대각선 복귀하도록 호출
                    t, d = self.calculate_travel_time(target_station, is_return=True) 
                    
                    self.warehouse.move_log.append({ # 분석을 위해 로봇의 스테이션 복귀 이력을 창고 로그에 추가
                        'time': self.env.now, # 현재 시뮬레이션 시간 기록
                        'ctu_id': self.id, # 복귀하는 로봇 ID 기록
                        'from_x': self.pos[0], 'from_y': self.pos[1], 'from_z': self.pos[2], # 마지막 피킹 지점 좌표 기록
                        'to_x': target_station[0], 'to_y': target_station[1], 'to_z': target_station[2], # 목적지 스테이션 좌표 기록
                        'item_id': 'RETURN', # 피킹 작업이 아닌 스테이션 복귀임을 의미하는 태그 삽입
                        'travel_time': t, 'travel_distance': d, # 복귀 구간 소요 시간 및 거리 기록
                        'dispatch_rule': self.dispatch_rule, # 당시 적용된 디스패칭 룰 기록
                    })
                    
                    yield self.env.timeout(t) # 복귀 시간만큼 시뮬레이션 시간 지연
                    self.pos = target_station # 복귀가 완료되었으므로 로봇 위치를 스테이션 좌표로 갱신
                    self.total_distance += d # 복귀 이동 거리 누적 더하기
                
                    # 모든 작업과 복귀를 마쳤으므로 점유했던 통로들을 깨끗하게 비워 해제
                    self.current_batch_aisles.clear()
                    self.busy = False # 로봇 상태를 다시 대기 중(False)으로 변경
                else: 
                    # 큐에 짐은 남아있으나, 다른 CTU가 모두 점유하고 있어 배치를 받지 못한 경우 1초간 대기하며 눈치보기
                    yield self.env.timeout(1) 
            else: # 아예 창고 큐 전체에 남은 할당 작업이 없는 경우
                yield self.env.timeout(1) # 무한 루프 과부하를 막기 위해 1초 대기 후 다시 작업 확인


# ==========================================
# 창고 시스템 클래스 정의
# ==========================================
class Warehouse: # 전체 창고, 재고, 큐, 로그를 통합 관리하는 클래스
    def __init__(self, env, inventory, plan_df): # 창고 초기화 메서드
        self.env = env # SimPy 시뮬레이션 환경 저장
        self.inventory = inventory # 슬로팅 룰에 의해 생성된 {부품: 위치} 맵핑 데이터 저장
        self.plan_df = plan_df # 금일 처리해야 할 전체 출고 계획 주문 데이터 저장
        self.job_queue = [] # 실시간으로 쏟아지는 주문(부품 피킹)들이 대기하는 리스트(큐) 초기화
        
        self.dispatch_log = [] # 디스패칭 룰에 의해 배치 작업이 어떻게 생성되었는지 기록할 리스트
        self.move_log = [] # 로봇의 점대점 이동 상세 이력을 기록할 리스트
        
        self.ctus = [] # 교착(Deadlock) 방지를 위해 창고가 전체 CTU들의 객체 정보를 알고 있도록 리스트 초기화

    def apply_dispatching(self, ctu, rule): # 룰에 따라 큐에서 작업을 꺼내 로봇에 할당해주는 메서드
        if not self.job_queue: return [] # 만약 큐가 비어있다면 빈 리스트 반환하여 작업 할당 취소

        # 병목 현상 방지: 현재 작업을 호출한 본인이 아닌 다른 CTU가 작업 중인 통로(X좌표) 목록을 수집
        occupied_aisles = set()
        for c in self.ctus:
            if c.id != ctu.id and c.busy: # 자신이 아니며, 현재 작업을 수행 중인 CTU인 경우
                occupied_aisles.update(c.current_batch_aisles) # 해당 CTU가 점유 중인 통로 목록을 블랙리스트(occupied_aisles)에 병합

        # 전체 대기 큐의 작업들 중에서 점유(블랙리스트)되지 않은 통로에 위치한 작업들만 골라 가용 풀을 생성
        # [새로운 수정사항] 대기 큐의 작업(부품) 좌표가 아닌, 해당 부품이 속한 '통로' 좌표를 기준으로 점유 검사 수행
        available_jobs = [j for j in self.job_queue if get_aisle_x(j['pos'][0]) not in occupied_aisles]

        # 만약 겹치지 않는 작업이 단 하나도 없다면 당장 할당을 포기하고 빈 리스트를 반환하여 안전하게 대기 유도
        if not available_jobs:
            return []

        # 1. Nearest
        if rule == 'nearest':
            # 소요 시간(Time) 기준으로 가장 빨리 도착할 수 있는 작업 정렬
            available_jobs.sort(key=lambda j: get_estimated_travel_time(ctu.pos, j['pos'])) 
            batch = [available_jobs.pop(0)] # 정렬된 가용 풀에서 제일 앞(가장 가까운) 작업을 Seed(기준 작업)로 뽑아 배치에 첫 번째로 담음
            
            if available_jobs: # 가용 풀에 남은 작업이 더 있다면
                available_jobs.sort(key=lambda j: get_estimated_travel_time(batch[0]['pos'], j['pos'])) # 방금 뽑은 Seed 작업 위치를 기준으로 가까운 순으로 재정렬
                extra_count = min(len(available_jobs), TOTE_CAPACITY - 1) # 남은 가용 작업 수와 로봇 여유 적재량 중 작은 값을 추가 작업 수로 확정
                batch.extend(available_jobs[:extra_count]) # Seed 주변의 추가 가용 작업들을 배치 리스트에 병합
                
            # 원본 job_queue에서 최종 할당된 배치의 작업들만 찾아내어 안전하게 삭제
            for j in batch:
                self.job_queue.remove(j)
                
            self.dispatch_log.append({ # 배치 구성 완료 후 배치 로그 기록
                'time': ctu.env.now, 'ctu_id': ctu.id, 
                'rule': rule,
                'seed_item': batch[0]['item_id'], 'batch_items': [j['item_id'] for j in batch], # 기준 부품과 전체 배치 부품 목록 기록
                'batch_size': len(batch), # 이 배치의 총 작업 수 기록
            })
            return batch # 완성된 묶음(배치) 반환

        # 2. Density
        elif rule == 'density':
            K = min(10, len(available_jobs)) # 가용 작업 중 로봇에서 가장 가까운 상위 10개만 Seed 후보로 추림
            candidate_seeds = sorted(available_jobs, key=lambda j: get_estimated_travel_time(ctu.pos, j['pos']))[:K] 
            
            best_seed = None # 최적의 Seed 작업을 저장할 변수 초기화
            max_density = -1 # 가장 높은 밀집도 점수를 저장할 변수 초기화
            r_y = 2.65 # 같은 통로 내에서 밀집도를 계산할 검색 반경(2.65m) 설정
            
            for seed in candidate_seeds: # 후보 Seed 10개 각각에 대해 주변 작업 밀집도 조사
                density = 0 # 현재 평가 중인 Seed의 밀집도 카운터 0으로 시작
                for job in available_jobs: # 밀집도를 계산할 때도 다른 로봇이 점유한 통로의 작업은 철저히 배제
                    if get_aisle_x(job['pos'][0]) == get_aisle_x(seed['pos'][0]): 
                        dy = abs(job['pos'][1] - seed['pos'][1]) # 같은 통로라면 Y좌표(깊이) 차이 계산
                        if dy <= r_y: # Y축 거리가 반경(2.65m) 이내라면
                            density += 1 # 주변 작업이므로 밀집도 카운트 1 증가
                
                if density > max_density: # 현재 계산한 밀집도가 역대 최대 밀집도보다 높다면
                    max_density = density # 최대 밀집도 갱신
                    best_seed = seed # 최적 Seed를 현재 Seed로 변경
                    
            available_jobs.sort(key=lambda j: get_estimated_travel_time(best_seed['pos'], j['pos'])) # 찾아낸 최적 밀집 Seed를 기준으로 가용 풀을 정렬
            
            batch_size = min(len(available_jobs), TOTE_CAPACITY) # 가용 남은 물량과 로봇 최대 용량 중 묶을 크기 결정
            best_batch = available_jobs[:batch_size] # 정렬된 가용 큐에서 위에서 결정한 개수만큼 묶음 생성
            
            # 원본 전체 job_queue에서 배치로 뽑혀나간 작업들을 삭제
            for j in best_batch:
                self.job_queue.remove(j)
            
            self.dispatch_log.append({ # 밀집도 기반 배치 생성 로그 기록
                'time': ctu.env.now, 'ctu_id': ctu.id, 
                'rule': rule,
                'seed_item': best_seed['item_id'], 'batch_items': [j['item_id'] for j in best_batch], # 기준 부품 및 배치 내 부품 리스트 기록
                'batch_size': len(best_batch), 'local_density': max_density, # 배치 크기 및 해당 지역의 계산된 밀집도 점수 기록
            })
            return best_batch # 생성된 밀집 배치 반환

        # 3. Load-balance
        elif rule == 'load_balance':
            K = min(10, len(available_jobs)) # 가용 풀 작업 중 가까운 상위 10개를 Seed 후보로 추출
            nearest_jobs = sorted(available_jobs, key=lambda j: get_estimated_travel_time(ctu.pos, j['pos'])) 
            candidate_seeds = nearest_jobs[:K] 
            
            best_batch = [] # 최단 시간으로 처리될 최적의 배치를 저장할 리스트 초기화
            min_eta = float('inf') # 최소 예상 소요 시간(ETA)을 무한대로 초기화 (비교 갱신용)
            best_seed_item = None # 최적 배치에 사용된 Seed 부품 코드를 저장할 변수 초기화
            
            for seed in candidate_seeds: # 10개의 각 후보 Seed를 가상으로 배치로 묶어봄
                temp_queue = sorted(available_jobs, key=lambda j: get_estimated_travel_time(seed['pos'], j['pos'])) 
                temp_batch = temp_queue[:TOTE_CAPACITY] # 임시 가용 큐에서 최대 용량만큼 가상 묶음 구성
                
                eta = 0 # 이 가상 배치를 처리할 때의 예상 소요 시간(ETA) 0으로 초기화
                current_pos = ctu.pos  # 로봇의 이동을 시뮬레이션하기 위해 가상 위치를 현재 로봇 위치로 설정
                
                temp_unvisited = temp_batch.copy()
                
                while temp_unvisited: 
                    temp_unvisited.sort(key=lambda j: get_estimated_travel_time(current_pos, j['pos']))
                    job = temp_unvisited.pop(0) # 최단 거리 작업 추출하여 가상 방문
                    
                    x1_raw, y1, z1 = current_pos # 가상 현재 좌표 언패킹
                    x2_raw, y2, z2 = job['pos'] # 가상 이동 목표 좌표 언패킹
                    
                    x1 = resolve_x(x1_raw)
                    x2 = resolve_x(x2_raw)
                    
                    xy_dist = compute_xy_dist(x1, y1, x2, y2) # 공용 헬퍼로 통일 (스테이션 관여 시 자동으로 대각선 단축 적용)
                        
                    dz = abs(z1 - z2) # 리프트 이동 거리 계산
                    time_to_job = max(xy_dist / CTU_SPEED_XY, dz / CTU_SPEED_Z) # 현재 지점에서 목표 작업까지의 시간 계산
                    eta += (time_to_job + 2.0) # 계산된 구간 시간 및 피킹 시간을 가상 총 소요 시간(ETA)에 누적
                    current_pos = (x2, y2, z2)  # 가상 로봇 위치를 방금 계산한 작업의 통로 지점으로 갱신
                
                # --- 스테이션 복귀 시간 ETA에 반영 ---
                last_x, last_y, last_z = current_pos
                target_station = get_nearest_station(last_x) # [레이아웃 확장 수정사항] 스테이션 개수 변화에 자동 대응
                
                return_xy_dist = compute_xy_dist(last_x, last_y, target_station[0], target_station[1])
                return_dz = abs(last_z - target_station[2])
                
                return_time = max(return_xy_dist / CTU_SPEED_XY, return_dz / CTU_SPEED_Z)
                eta += return_time # 총 소요 시간에 복귀 시간을 합산해야 진짜 Makespan 산출이 가능
                
                if eta < min_eta: # 이 가상 배치의 총 예상 시간이 이전 최소값보다 더 짧다면
                    min_eta = eta # 최소 예상 시간 갱신
                    best_batch = temp_batch # 최적의 배치를 방금 만든 가상 배치로 교체
                    best_seed_item = seed['item_id'] # 그때 사용된 Seed 부품명 저장
            
            # 최종 확정된 배치의 부품들을 진짜 대기 큐(job_queue)에서 안전하게 지우기
            for job in best_batch: 
                self.job_queue.remove(job) 
                
            self.dispatch_log.append({ # 로드 밸런스 기반 배치 생성 로그 기록
                'time': ctu.env.now, 'ctu_id': ctu.id, 
                'rule': rule,
                'seed_item': best_seed_item, 'batch_items': [j['item_id'] for j in best_batch], # Seed 정보와 할당 부품 리스트 기록
                'batch_size': len(best_batch), 'eta': min_eta, # 배치 크기와 시뮬레이션 전 예측했던 예상 종료 시간(ETA) 기록
            })
            return best_batch # 가장 빨리 끝나는 최적 배치 반환

# 슬로팅 로직 수정 
def generate_fixed_inventory(rule_type, part_list, weekly_prod_plan, bom_df, outbound_plan, seed=1234):
    """
    1. 제품별 생산 계획(물량)을 고려
    2. kitting 시 같은 제품의 부품들을 물리적으로 인접하게 배치
    """
    random.seed(seed)
    inventory = {}
    
    # 1. 제품별 주간 생산 물량 계산
    prod_vol = weekly_prod_plan.groupby('제품코드').size().to_dict()
    # 물량이 많은 순서대로 제품 정렬
    sorted_products = sorted(prod_vol.keys(), key=lambda x: prod_vol.get(x, 0), reverse=True)

    # 2. 가용 슬롯 생성 및 정렬 (스테이션에서 가까운 순)
    # [수정] PPT 기획안 기준 완벽 동기화: 랙 1개 길이에 16개의 박스(Bin) 적재
    # 통로(Aisle)를 따라 로봇이 정차하는 Y축 위치를 현실의 박스 규격(600mm)에 맞춰 정확히 16등분
    y_positions = np.linspace(RACK_START_Y, RACK_END_Y, num=16)
    candidate_slots = []
    RACK_OFFSET = 0.9  # 랙 중심에서 좌/우로 떨어진 X좌표 간격 (실제 랙 폭과 통로 폭을 고려하여 0.9m로 설정) (추가)
    for x in AISLE_X:
        for y in y_positions:
            for z in SHELF_HEIGHTS:
                # [수정] 좌/우 양면 랙(Back-to-Back) 동시 커버 로직
                # 로봇이 하나의 통로 X에 진입하여 특정 Y, Z 좌표에 멈추면,
                # 좌측 랙과 우측 랙 양쪽의 Bin을 모두 사용할 수 있으므로 2개의 가용 슬롯을 생성합니다.
                # 총 슬롯 수 = 18개 통로(3블록 × 6) * 16개 Y위치 * 7층 * 2(좌우) = 정확히 4,032 Bin 확보 [레이아웃 확장 수정사항]
                # [수정] 물리적 좌표를 다르게 설정 (왼쪽 랙은 -, 오른쪽 랙은 +)
                # 나중에 'L'/'R' 어디쪽 랙에 피킹할지에 대한 지시어 추가
                candidate_slots.append((round(x - RACK_OFFSET, 2), round(y, 2), z)) # 왼쪽 랙
                candidate_slots.append((round(x + RACK_OFFSET, 2), round(y, 2), z)) # 오른쪽 랙

    def est_time_to_station(pos):
        x, y, z = pos
        min_time = float('inf')
        for st in STATIONS.values():
            dist = compute_xy_dist(x, y, st[0], st[1])
            t = max(dist / CTU_SPEED_XY, abs(z - st[2]) / CTU_SPEED_Z)
            if t < min_time: min_time = t
        return min_time

    candidate_slots.sort(key=est_time_to_station)

    # 3. 전략별 부품 배치 순서 결정 (Parts Ordering)
    parts_order = []
    used_parts = set()

    # generate_fixed_inventory 함수 내부의 Kitting 전략 부분 수정 제안
    # --- generate_fixed_inventory 함수 내부의 kitting 조건절 수정 ---
    if rule_type == "kitting":
        # --- [단계 1] 공용 자재(Common Parts) 식별 (Hybrid 전략) ---
        # 2개 이상의 제품(Product)에 들어가는 자재는 '중앙/전진 배치' 대상
        part_usage = bom_df.groupby('part_code')['product_code'].nunique()
        common_parts = part_usage[part_usage >= 2].index.tolist()
        
        # --- [단계 2] 전용 자재(Specific Parts) 간의 출고 연관성 계산 ---
        # 공용 자재를 제외한 나머지 부품들로만 키팅 분석 진행
        specific_parts_only = [p for p in part_list if p not in common_parts]
        
        pair_counter = Counter()
        for _, group in outbound_plan.groupby('No.'):
            # 한 주문 내에서 공용 부품을 제외한 전용 부품들만 추출
            parts = sorted([p for p in group['부품코드'].unique() if p in specific_parts_only])
            if len(parts) > 1:
                # 주문 내에 함께 존재하는 부품 쌍 생성
                for pair in combinations(parts, 2):
                    pair_counter[pair] += 1
        
        # --- [단계 3] 클러스터 생성 (최대 7개 제한) ---
        clusters = []       # 생성된 부품 뭉치들 저장
        assigned_parts = set() # 이미 클러스터에 배정된 부품 추적
        
        # 함께 자주 나가는 순서대로 정렬된 쌍(Pair) 리스트
        sorted_pairs = [pair for pair, count in pair_counter.most_common()]
        
        for p1, p2 in sorted_pairs:
            # 1. 둘 다 처음 보는 부품인 경우 -> 새로운 클러스터 생성
            if p1 not in assigned_parts and p2 not in assigned_parts:
                clusters.append([p1, p2])
                assigned_parts.update([p1, p2])
            
            # 2. p1만 이미 어떤 클러스터에 있는 경우 -> p2를 그 클러스터에 추가 (여유 있으면)
            elif p1 in assigned_parts and p2 not in assigned_parts:
                for cluster in clusters:
                    if p1 in cluster and len(cluster) < TOTE_CAPACITY: # 7개 제한 확인
                        cluster.append(p2)
                        assigned_parts.add(p2)
                        break
            
            # 3. p2만 이미 어떤 클러스터에 있는 경우 -> p1을 그 클러스터에 추가 (여유 있으면)
            elif p2 in assigned_parts and p1 not in assigned_parts:
                for cluster in clusters:
                    if p2 in cluster and len(cluster) < TOTE_CAPACITY: # 7개 제한 확인
                        cluster.append(p1)
                        assigned_parts.add(p1)
                        break
        
        # --- [단계 4] 최종 배치 순서 조립 (짧은 시간 슬롯부터 채우기) ---
        parts_order = []
        
        # 1순위: 공용 부품 (가장 접근성 좋은 스테이션 근처 중앙 슬롯 차지)
        parts_order.extend(common_parts)
        
        # 2순위: 연관성 클러스터 (함께 나가는 부품들을 물리적으로 나란히 배치)
        for cluster in clusters:
            parts_order.extend(cluster)
            
        # 3순위: 나머지 묶이지 않은 전용 부품들
        remaining_parts = [p for p in part_list if p not in common_parts and p not in assigned_parts]
        parts_order.extend(remaining_parts)

    elif rule_type == "characteristic":
        # 생산 물량이 가장 많은 Top 제품의 부품들을 전진 배치
        # 단일 1위 제품이 아닌, 파레토 법칙에 기반해 상위 20% 제품군으로 기준 확대
        if sorted_products:
            top_n = max(1, int(len(sorted_products) * 0.2)) # 전체 제품 종류 중 상위 20% 개수 산출 (최소 1개 보장)
            top_products = sorted_products[:top_n] # 상위 20% 제품 리스트 추출
            
            # 상위 제품군들에 포함되는 모든 부품 코드를 집합(Set)으로 추출하여 중복 제거
            top_parts = set(bom_df[bom_df['product_code'].isin(top_products)]['part_code'].unique())
            
            parts_order = [p for p in part_list if p in top_parts] + \
                          [p for p in part_list if p not in top_parts]
        else:
            parts_order = list(part_list)

    elif rule_type == "abc":
        part_demand = {}
        for p_code, vol in prod_vol.items():
            p_parts = bom_df[bom_df['product_code'] == p_code]['part_code'].unique()
            for p in p_parts:
                part_demand[p] = part_demand.get(p, 0) + vol
        parts_order = sorted(part_list, key=lambda x: part_demand.get(x, 0), reverse=True)

    else: # random
        parts_order = list(part_list)
        random.shuffle(parts_order)

    # 4. 부품과 슬롯 매핑 (1:1)
    for part, slot in zip(parts_order, candidate_slots):
        inventory[part] = list(slot)

    return inventory

# ==========================================
# 하루 단위 시뮬레이션 실행
# ==========================================
def run_simulation(date, inventory, dispatch_rule, date_plan): # 특정 일자의 작업을 시뮬레이션하는 메인 구동 함수
    env = simpy.Environment() # 이산 사건 시뮬레이션의 근간이 되는 SimPy 환경 객체 생성
    warehouse = Warehouse(env, inventory, date_plan) # 위에서 만든 재고와 일일 계획을 바탕으로 창고 객체 인스턴스화

    ctus = [CTU(env, i, warehouse, dispatch_rule) for i in range(NUM_CTUS)] # 창고 내에서 주어진 디스패칭 룰에 따라 지정된 대수의 CTU 객체 생성
    warehouse.ctus = ctus # 창고 시스템이 전체 로봇의 위치(점유 통로)를 알 수 있도록 방금 생성한 CTU 리스트를 창고 속성에 주입
    
    def job_gen(env, wh): # 계획에 맞춰 주문 발생을 모사하는 내부 제너레이터 함수
        # 일관된 결과를 위해 각 날짜마다 시드를 일정하게 맞추어 주문 발생 타이밍 통제
        random.seed(hash(date) % 10000) 
        
        for _, group in wh.plan_df.groupby('No.'): # 일일 주문 계획을 주문번호(No.) 별로 묶어 순회
            yield env.timeout(random.uniform(2, 5)) 
            for _, row in group.iterrows(): # 하나의 주문 묶음 내 포함된 부품 리스트 순회
                p_id = row['부품코드'] # 작업해야 할 부품 코드 추출
                if p_id in wh.inventory: # 재고 맵에 이 부품의 위치가 존재하는지 안전 검사
                    wh.job_queue.append({'item_id': p_id, 'pos': wh.inventory[p_id], 'time': env.now}) # 창고 대기 큐에 이 부품의 위치, 주문 발생 시간 기록

    def monitor(env, warehouse, gen_proc, ctus): # 시뮬레이션 종료 시점을 감시하는 내부 함수
        yield gen_proc # 주문 발생 제너레이터(job_gen)가 모든 주문 인입을 끝낼 때까지 대기
        while warehouse.job_queue or any(c.busy for c in ctus): # 주문 생성이 끝난 후에도 창고 큐에 작업이 남아있거나, 아직 작업 중인 로봇이 있다면
            yield env.timeout(1)  # 1초씩 시간을 흐르게 하며 모니터링 반복 루프 지속 (모두 종료되면 루프 탈출)

    gen_proc = env.process(job_gen(env, warehouse)) # 주문 발생 제너레이터를 SimPy 프로세스로 등록
    env.run(until=env.process(monitor(env, warehouse, gen_proc, ctus))) # 모니터 함수(프로세스)가 종료될 때까지 전체 시뮬레이션 엔진 가동
    
    return { # 하루치 시뮬레이션이 끝난 뒤 분석용 데이터 반환
        'Date': date,  # 시뮬레이션 실행 날짜
        'Time': round(env.now, 1),  # 시뮬레이션의 마지막 순간(총 작업 소요 시간) 반환
        'Distance': round(sum(c.total_distance for c in ctus), 1), # 두 로봇의 이동 거리 총합산 반환
        'Slotting': None,      # 메인에서 채워넣을 예정
        'Dispatching': None,   # 메인에서 채워넣을 예정
        'Move_log': pd.DataFrame(warehouse.move_log), # 쌓인 로봇 이동 이력 로그를 데이터프레임으로 변환하여 반환
        'Dispatch_log': pd.DataFrame(warehouse.dispatch_log), # 쌓인 배치 할당 로그 데이터프레임 변환 반환
    }

# ==========================================
# 개별 알고리즘 조합 통합 시각화 
# ==========================================
def visualize_rule_combination(move_all_df, slot_rule, disp_rule): # 호출 시마다 특정 슬로팅/디스패칭 조합을 시각화하도록 매개변수 추가
    """
    입력받은 슬로팅(slot_rule)과 디스패칭(disp_rule) 조합의 시뮬레이션 결과를 
    3가지 방식(3D Scatter, 2D Trajectory, Gantt Chart)으로 시각화합니다.
    (기존 Kitting + Load_balance 전용 함수를 모든 조합에 범용적으로 쓸 수 있게 확장)
    """
    # 1. 데이터 필터링: 입력받은 조합(slot_rule, disp_rule) 추출
    target_df = move_all_df[(move_all_df['Slotting'] == slot_rule) &  # 전체 이력에서 슬로팅 룰 조건 필터링
                            (move_all_df['Dispatching'] == disp_rule)].copy() # 디스패칭 룰 조건까지 AND 결합하여 복사
    
    if target_df.empty: # 해당 조합의 데이터가 비어있다면
        print(f"해당 조건({slot_rule} + {disp_rule})의 데이터가 없습니다.") # 에러 메시지 출력 후 스킵
        return # 함수 종료
        
    # 시각화가 너무 복잡해지는 것을 막기 위해 첫 번째 시뮬레이션 날짜만 선택
    sample_date = target_df['Date'].iloc[0] # 타겟 데이터프레임에서 가장 첫 번째 줄의 시뮬레이션 날짜 문자열 추출
    df_day = target_df[target_df['Date'] == sample_date].copy() # 전체 데이터 중 방금 추출한 단일 날짜의 결과만 필터링하여 복사
    
    print(f"\n=== [시각화 진행 중: Date {sample_date} | {slot_rule} × {disp_rule}] ===") # 현재 시각화하는 조합과 날짜 정보를 터미널에 명확히 안내

    # ---------------------------------------------------------
    # [1] 3D Scatter Plot: 부품 피킹 위치 분포
    # ---------------------------------------------------------
    fig1 = plt.figure(figsize=(10, 8)) # 3D 산점도를 그리기 위해 가로 10, 세로 8 크기의 새 피규어 생성
    ax1 = fig1.add_subplot(111, projection='3d') # 위 피규어의 첫 번째 영역에 3D 투영(projection)을 활성화한 축 객체 생성
    picks = df_day[df_day['item_id'] != 'RETURN'] # 스테이션 복귀는 제외하고 실제 부품 좌표 데이터만 추출
    pick_counts = picks.groupby(['to_x', 'to_y', 'to_z', 'item_id']).size().reset_index(name='count') # 동일한 X,Y,Z 위치에 피킹이 몇 번 일어났는지 집계하여 count 열로 명명
    
    scatter = ax1.scatter(pick_counts['to_x'], pick_counts['to_y'], pick_counts['to_z'],  # 추출한 x, y, z 좌표 리스트를 기반으로 3D 산점도 그리기
                          s=pick_counts['count']*50, c=pick_counts['to_z'], cmap='viridis', alpha=0.7) # 빈도(count)가 클수록 점 크기(s) 확대, 층수(Z)에 따라 색상 분리, 반투명도 지정
    
    ax1.set_title(f'① 3D Scatter: 피킹 위치 분포 ({slot_rule} × {disp_rule})', fontsize=14, fontweight='bold') # 3D 플롯 제목에 현재 적용 중인 룰 변수를 동적 삽입
    ax1.set_xlabel('통로 X (Aisle)') # 3D 플롯 X축 레이블 지정
    ax1.set_ylabel('랙 깊이 Y') # 3D 플롯 Y축 레이블 지정
    ax1.set_zlabel('선반 높이 Z') # 3D 플롯 높이 Z축 레이블 지정
    fig1.colorbar(scatter, ax=ax1, label='Z축 높이 (층수)') # Z 높이에 따른 범례 역할을 하는 컬러바를 우측에 부착
    plt.tight_layout() # 요소 간 겹침 방지 여백 조정
    plt.show() # 3D 산점도 화면 출력

    # ---------------------------------------------------------
    # [2] 2D Trajectory Map: 로봇 이동 궤적 밀집도 (Heatmap 스타일 렌더링)
    # ---------------------------------------------------------
    fig2, ax2 = plt.subplots(figsize=(26, 8)) # [레이아웃 확장 수정사항] 3개 블록(가로로 3배 넓어진 레이아웃)이 잘리지 않도록 피규어 가로 폭을 12 -> 26으로 확대
    
    # 랙 및 스테이션 배경 그리기: 기획안 도면(R01 ~ R12)과 완전히 동일한 X좌표와 폭(Width)으로 백투백(Back-to-Back) 랙 덩어리들을 생성
    # [레이아웃 확장 수정사항] 기존 1개 블록짜리 rack_spans를 NUM_BLOCKS(3)개만큼 옆으로 복제하여
    # 붙어있는 연속 구조(통로 6개 -> 18개)를 그대로 시각화에도 반영
    base_rack_spans = [(0, 0.6), (1.8, 1.2), (4.2, 1.2), (6.6, 1.2), (9.0, 1.2), (11.4, 1.2), (13.8, 0.6)] # 블록 1개 내부의 구조: (단일 0.6m) - (양면 1.2m) * 5 - (단일 0.6m)
    rack_spans = [] # 3개 블록 전체의 랙 좌표를 담을 리스트
    for _b in range(NUM_BLOCKS):
        _offset = _b * (BLOCK_WIDTH + BLOCK_GAP) # 블록 순서에 따라 오른쪽으로 밀어낼 X 오프셋
        rack_spans.extend([(round(sx + _offset, 2), w) for sx, w in base_rack_spans])

    # [수정사항] 기존처럼 랙에도 번호(R01~R36)를 순서대로 부여. 폭 1.2m 랙은 양면(좌/우 두 면에서 피킹 가능)이라
    # 번호를 2개, 폭 0.6m 랙은 단면이라 번호를 1개씩 왼쪽부터 순서대로 매김
    rack_labels = [] # [(start_x, width, [부여된 랙 번호 목록]), ...]
    _rack_id = 1
    for start_x, width in rack_spans:
        if width >= 1.0: # 1.2m 양면 랙 -> 좌/우 두 면에 번호 2개 부여
            ids = [f'R{_rack_id:02d}', f'R{_rack_id + 1:02d}']
            _rack_id += 2
        else: # 0.6m 단면 랙 -> 번호 1개만 부여
            ids = [f'R{_rack_id:02d}']
            _rack_id += 1
        rack_labels.append((start_x, width, ids))

    _rack_mid_y = (RACK_START_Y + RACK_END_Y) / 2 # 랙 번호 텍스트를 랙 세로 한가운데에 배치하기 위한 기준 Y좌표
    for (start_x, width), (_, _, ids) in zip(rack_spans, rack_labels): # 랙 덩어리 좌표 배열을 순회
        ax2.add_patch(patches.Rectangle((start_x, RACK_START_Y), width, RACK_END_Y - RACK_START_Y, color='lightgray')) # 도면에 맞게 회색 랙 덩어리 배치
        if len(ids) == 2: # 양면 랙이면 좌측 1/4, 우측 3/4 지점에 각각 번호 표기
            ax2.text(start_x + width * 0.25, _rack_mid_y, ids[0], ha='center', va='center', fontsize=6, rotation=90)
            ax2.text(start_x + width * 0.75, _rack_mid_y, ids[1], ha='center', va='center', fontsize=6, rotation=90)
        else: # 단면 랙이면 정중앙에 번호 1개만 표기
            ax2.text(start_x + width * 0.5, _rack_mid_y, ids[0], ha='center', va='center', fontsize=6, rotation=90)

    for x in AISLE_X: # 통로 라벨 추가
        ax2.text(x, RACK_END_Y + 0.5, AISLE_LABELS[x], ha='center', fontsize=9) # [수정사항] 통로 좌표값 대신 통로 번호(A01~A18) 표기
        
    ax2.axhline(CROSS_AISLE_Y, color='black', linestyle='--', alpha=0.3, label='교차 통로 (Cross Aisle)') # 로봇이 교차하여 지나가는 Y축 위치에 검은 점선 가이드라인 표시
    
    # 스테이션 개수와 무관하게 동작하도록 STATIONS 딕셔너리를 순회하며 그림 (현재는 S01, S02 2개)
    for _i, (_name, _pos) in enumerate(STATIONS.items()):
        _label = 'Station' if _i == 0 else None # 범례 중복 방지를 위해 첫 스테이션에만 라벨 부여
        ax2.add_patch(patches.Rectangle((_pos[0] - 1.0, 0), 2.0, 1.0, color='lightgreen', label=_label)) # 스테이션 위치에 연녹색 박스 배치
        ax2.text(_pos[0], 0.5, _name, ha='center', va='center', fontweight='bold') # 박스 중앙에 스테이션 이름 텍스트 추가

    # ----- 핵심 로직: 궤적을 5cm 단위의 점(Point)으로 쪼개 통행량 카운트 -----
    traffic_counts = {} # 특정 (x, y) 좌표를 지날 때마다 누적 횟수를 저장할 빈 딕셔너리 생성

    def add_line_segment(sx, sy, ex, ey): # 시뮬레이션의 선형 궤적을 점 단위로 분해하는 내부 함수
        """시작점과 끝점을 받아 그 사이의 경로를 5cm 간격의 점으로 변환 후 카운트 증가"""
        dist = math.sqrt((sx - ex)**2 + (sy - ey)**2) 
        steps = int(dist / 0.05) + 1 # 0.05m(5cm) 해상도로 경로를 쪼개어 생성할 점의 전체 개수 계산
        for i in range(steps): # 계산된 점의 개수만큼 루프 반복
            px = sx + (ex - sx) * i / max(1, steps - 1) # i번째 점의 X 좌표 보간 계산
            py = sy + (ey - sy) * i / max(1, steps - 1) # i번째 점의 Y 좌표 보간 계산
            coord = (round(px, 2), round(py, 2)) # 계산된 점을 소수점 2자리로 정규화하여 튜플 생성
            traffic_counts[coord] = traffic_counts.get(coord, 0) + 1 # 딕셔너리에 해당 좌표 키가 있으면 값을 1 증가, 없으면 1로 초기화 (통행 빈도 누적)
            
    # 로봇의 모든 점대점 이동 이력 순회
    for _, row in df_day.iterrows(): # 해당 날짜 로봇의 모든 이동 이력을 한 줄씩 순회
        # [수정사항] 시각화 맵에서도 부품 슬롯 위치는 통로 중앙 X좌표로, 스테이션 위치는 그대로(resolve_x)
        x1 = resolve_x(row['from_x']) 
        y1 = row['from_y'] # 출발점 Y 좌표 추출
        x2 = resolve_x(row['to_x']) 
        y2 = row['to_y'] # 도착점 Y 좌표 추출
        
        x1_is_station = round(x1, 6) in STATION_XS
        x2_is_station = round(x2, 6) in STATION_XS
        
        if x1_is_station and not x2_is_station:
            # [수정사항] 스테이션에서 출발하는 경우(배치 첫 이동): 복귀와 대칭으로,
            # 스테이션에서 목표 통로 진입점(X2, 교차통로)까지 대각선으로 곧장 그은 뒤 통로 안쪽으로 직진
            add_line_segment(x1, y1, x2, CROSS_AISLE_Y) # 스테이션에서 목표 통로 진입점까지 대각선 횡단
            add_line_segment(x2, CROSS_AISLE_Y, x2, y2) # 통로 안쪽으로 진입
        elif x2_is_station and not x1_is_station:
            # 복귀 궤적: 랙에서 빠져나와 교차통로(Y=3.0)까지는 직진 이동 후, 스테이션까지 대각선 궤적을 추가
            add_line_segment(x1, y1, x1, CROSS_AISLE_Y) # 랙에서 교차 통로로 나오기
            add_line_segment(x1, CROSS_AISLE_Y, x2, y2) # 교차 통로에서 스테이션으로 대각선 횡단
        else:
            if x1 == x2: # 출발 통로와 도착 통로가 같아 꺾을 필요가 없다면 (한 번에 직진)
                add_line_segment(x1, y1, x2, y2) # 해당 직진 구간 전체를 점으로 쪼개 카운트 등록
            else: # 다른 통로로 이동하는 경우 (교차통로 우회 3단계 궤적, 랙을 통과하지 않도록)
                add_line_segment(x1, y1, x1, CROSS_AISLE_Y)        # 1. 현재 랙 안쪽에서 교차로 지점(Y=3.0)까지 후퇴하는 직선 구간 카운트
                add_line_segment(x1, CROSS_AISLE_Y, x2, CROSS_AISLE_Y) # 2. 교차로 지점 내에서 목표 통로(X2)까지 가로로 횡단하는 구간 카운트
                add_line_segment(x2, CROSS_AISLE_Y, x2, y2)        # 3. 목표 통로 교차로에서 최종 랙 안쪽(Y2)으로 깊숙이 진입하는 직선 구간 카운트

    # 수집된 통행 빈도 데이터를 시각화를 위한 리스트로 분리
    pts_x, pts_y, pts_c = [], [], [] # X좌표 리스트, Y좌표 리스트, 빈도수(색상값) 리스트 초기화
    for (x, y), count in traffic_counts.items(): # 누적된 통행 딕셔너리를 순회
        pts_x.append(x) # 분리된 X 리스트에 추가
        pts_y.append(y) # 분리된 Y 리스트에 추가
        pts_c.append(count) # 빈도수를 리스트에 추가

    # -------------------------------------------------------------------
    # Scatter Plot을 활용해 선 대신 연속된 점들로 그라데이션(Heatmap) 렌더링
    # cmap='Reds': 한 번 지나가면 연한 핑크, 여러 번 지나가 병목인 곳은 진한 빨강
    # -------------------------------------------------------------------
    sc = ax2.scatter(pts_x, pts_y, c=pts_c, cmap='Reds', s=15, zorder=3, alpha=0.9) # 추출한 리스트를 산점도로 그리되 빈도(pts_c)에 따라 빨간색 농도를 다르게 적용
    
    # 우측에 컬러바를 부착하여 어떤 색상이 얼마나 많은 횟수를 의미하는지 범례 제공
    cbar = fig2.colorbar(sc, ax=ax2) # 방금 그린 산점도 색상 기준을 축으로 삼아 컬러바 생성
    cbar.set_label('로봇 누적 통행 빈도 (Traffic Intensity)') # 컬러바 옆 설명 텍스트 부착

    ax2.set_title(f'② 2D Trajectory Map: 로봇 통행 밀집도 궤적 ({slot_rule} × {disp_rule})', fontsize=14, fontweight='bold') # 2D 궤적 맵 제목에 현재 적용 중인 조합 변수명 동적 표시
    ax2.set_xlabel('X 좌표 (m)') # 궤적 맵 X축 물리적 단위(미터) 라벨
    ax2.set_ylabel('Y 좌표 (m)') # 궤적 맵 Y축 라벨
    ax2.set_xlim(-1, NUM_BLOCKS * BLOCK_WIDTH + (NUM_BLOCKS - 1) * BLOCK_GAP + 1.5) # [레이아웃 확장 수정사항] 3개 블록 전체 폭에 맞게 X축 오른쪽 여백 확대
    ax2.set_ylim(-1, 17) # 하단 스테이션 영역과 랙 상단 여백 표시를 위해 Y 범위 -1 ~ 17 확보
    
    # 기존 범례를 깔끔하게 유지하기 위한 정리 로직
    handles, labels = ax2.get_legend_handles_labels() # 플롯에 추가된 모든 막대 종류의 범례 핸들과 라벨 텍스트 수집
    by_label = dict(zip(labels, handles)) # 중복 라벨을 하나로 합치기 위해 딕셔너리 구조 활용
    ax2.legend(by_label.values(), by_label.keys(), loc='upper left') # 좌측 상단에 정돈된 범례 표시
    
    plt.tight_layout() # 간격 자동 조절
    plt.show() # 2D 궤적 맵 화면 출력

    # ---------------------------------------------------------
    # [3] Gantt Chart: CTU별 작업 시간 및 유휴 시간 확인
    # ---------------------------------------------------------
    fig3, ax3 = plt.subplots(figsize=(12, 4)) # 가로로 긴 형태(12x4)의 간트 차트 피규어 생성
    # -------------------------------------------------------------------------
    # [수정사항] CTU 대수가 스테이션 수(NUM_CTUS)만큼 늘어난 것에 맞춰
    # 간트 차트도 하드코딩된 2대(0,1)가 아니라 warehouse.ctus 전체 대수에 맞게
    # 동적으로 행(Row)을 생성하도록 일반화했습니다.
    # -------------------------------------------------------------------------
    ctu_ids = sorted(df_day['ctu_id'].unique().tolist())
    ctu_tasks = {cid: [] for cid in ctu_ids} # 각 로봇의 (작업 시작 시점, 작업 소요 시간) 정보 튜플을 담을 빈 리스트 딕셔너리
    ctu_returns = {cid: [] for cid in ctu_ids} # 복귀 구간도 별도의 색상으로 구분하기 위해 (시작, 소요) 튜플을 담을 딕셔너리
    
    for _, row in df_day.iterrows(): # 모든 이동 이력을 시간순으로 순회
        end_time = row['time'] # 해당 구간 이동이 끝난 완료 시간
        duration = row['travel_time'] # 해당 구간 이동에 걸린 소요 시간(초)
        start_time = end_time - duration # 이동 완료 시점에서 소요 시간을 빼 이동이 시작된 출발 시간 역산
        
        if row['item_id'] == 'RETURN': # 만약 이동 목적이 작업이 아닌 스테이션 복귀라면
            ctu_returns[row['ctu_id']].append((start_time, duration)) # 복귀 리스트에 시작, 지속시간 튜플 저장
        else: # 일반 부품 피킹 이동이라면
            ctu_tasks[row['ctu_id']].append((start_time, duration)) # 작업 리스트에 시작, 지속시간 저장
    
    task_colors = plt.cm.tab10.colors # 팔레트에서 CTU별로 서로 다른 색상을 순환 사용
    for idx, cid in enumerate(ctu_ids):
        y_base = 5 + idx * 10 # CTU마다 세로 위치를 10칸씩 벌려서 배치
        task_color = task_colors[idx % len(task_colors)]
        ax3.broken_barh(ctu_tasks[cid], (y_base, 8), facecolors=task_color, label=f'CTU {cid} (피킹)')
        ax3.broken_barh(ctu_returns[cid], (y_base, 8), facecolors=task_color, alpha=0.4, label=f'CTU {cid} (복귀)')
    
    ax3.set_ylim(0, 5 + len(ctu_ids) * 10) # CTU 대수에 맞춰 Y축 범위 동적 확장
    # [새로운 수정사항] 전체 시간으로 하면 바코드처럼 보이므로 첫 1,000초 구간만 확대해서 보여주기 (기존 코드: df_day['time'].max() + 10)
    ax3.set_xlim(0, 1000) 
    ax3.set_yticks([5 + idx * 10 + 4 for idx in range(len(ctu_ids))]) # Y축 좌측 라벨이 각 막대의 정중앙을 가리키도록 틱 위치 지정
    ax3.set_yticklabels([f'CTU {cid}' for cid in ctu_ids]) # 지정된 틱 위치에 로봇 이름 텍스트 표시
    ax3.set_xlabel('시뮬레이션 시간 (초)') # X축이 시간 흐름임을 명시
    ax3.set_title(f'③ Gantt Chart: CTU 작업 흐름 ({slot_rule} × {disp_rule})', fontsize=14, fontweight='bold') # 간트 차트 제목에 조합 변수명 동적 표시
    
    handles, labels = ax3.get_legend_handles_labels() # 플롯에 추가된 모든 막대 종류의 범례 핸들과 라벨 텍스트 수집
    by_label = dict(zip(labels, handles)) # 중복 라벨을 하나로 합치기 위해 딕셔너리 구조 활용 (같은 이름이면 덮어씀)
    ax3.legend(by_label.values(), by_label.keys(), loc='upper right') # 중복 제거된 유니크한 범례를 우측 상단에 박스로 표시
    
    plt.tight_layout() # 차트 여백 최적화
    plt.show() # 최종 간트 차트 팝업 출력

# ==========================================
# 주 단위 런타임(소요 시간) 시각화 표 생성 함수 [수정]
# ==========================================
def report_weekly_scoreboard(res_df):
    """모든 조합의 일별 런타임을 주 단위로 합산하여 히트맵(Heatmap) 형태의 시각화 표로 출력합니다."""
    
    # 1. Date 컬럼을 datetime 타입으로 확실히 변환 (주차별 그룹화를 위해 필요)
    res_df['Date'] = pd.to_datetime(res_df['Date'])
    
    # ISO 캘린더 기준으로 연도와 주차(Week)를 추출하여 라벨용 파생 변수(Week) 생성 (예: '2026년 01주차')
    res_df['Week'] = res_df['Date'].dt.strftime('%G년 %V주차')
    
    # 시각화 표의 Y축(행)에 표시될 이름 구분을 위해 슬로팅 룰과 디스패칭 룰을 합친 조합(Combo) 파생 변수 생성
    res_df['Combo'] = res_df['Slotting'] + " + " + res_df['Dispatching']
    
    # [추가] 런타임 보드를 오름차순으로 정렬하기 위한 로직
    # 각 조합(Combo)별로 전체 기간 동안의 총 소요 시간을 합산합니다.
    total_time_per_combo = res_df.groupby('Combo')['Time'].sum()
    # 총 소요 시간이 가장 적은 순서(오름차순)대로 조합명(index)을 정렬하여 리스트로 추출합니다.
    sorted_combos = total_time_per_combo.sort_values(ascending=True).index
    
    # 2. 주 단위 런타임 피벗 테이블 생성 
    # - index(행)에는 알고리즘 조합명을, columns(열)에는 계산된 주차 라벨을 배치
    # - values에는 기존 시뮬레이션 소요 시간(Time)을 넣되, aggfunc='sum'을 통해 해당 주차의 모든 일자 소요 시간을 합산
    weekly_pivot = res_df.pivot_table(index='Combo', columns='Week', values='Time', aggfunc='sum').round(1)
    
    # [추가] 생성된 피벗 테이블의 행(index)을 위에서 구한 오름차순 조합 리스트(sorted_combos) 순서대로 재배열합니다.
    # 이렇게 하면 가장 빨리 끝나는(효율적인) 알고리즘 조합이 표의 맨 위에 위치하게 됩니다.
    weekly_pivot = weekly_pivot.reindex(sorted_combos)
    
    # 3. 히트맵(Heatmap) 시각화 표 출력 영역 설정 (가로 14, 세로 8 크기)
    plt.figure(figsize=(14, 8))
    
    # Seaborn 히트맵 생성:
    # - annot=True를 통해 표의 각 셀 안에 실제 합산된 런타임 숫자(초)를 텍스트로 바로 표시
    # - fmt=".1f"를 통해 소수점 첫째 자리까지만 깔끔하게 출력
    # - cmap="YlOrRd" 설정으로 숫자가 클수록(오래 걸릴수록) 노랑 -> 주황 -> 빨강으로 직관적인 색상 변화 부여
    sns.heatmap(weekly_pivot, annot=True, fmt=".1f", cmap="YlOrRd", 
                linewidths=0.5, cbar_kws={'label': '주간 총 소요 시간 (초)'})
    
    # 시각화 표의 제목 및 축 레이블 설정
    plt.title('알고리즘 조합별 주간 시뮬레이션 총 소요 시간 (단위: 초) - 오름차순 정렬', fontsize=16, fontweight='bold', pad=20)
    plt.xlabel('생산 주차 (Week)', fontsize=12)
    plt.ylabel('알고리즘 조합 (Slotting + Dispatching)', fontsize=12)
    
    # Y축 라벨(알고리즘 조합명) 글자가 기본적으로 세로로 누워있는 것을 막기 위해 가로(0도)로 회전
    plt.yticks(rotation=0) 
    
    # 범례와 표가 겹치지 않도록 전체적인 간격 자동 최적화
    plt.tight_layout()
    # 화면에 실제 시각화 표 팝업 출력
    plt.show()

def report_kpi_scoreboard(res_df):
    """31일간의 알고리즘 성과를 수치로 요약 (KPI) - 1등 색상 강조 버전"""
    import os
    os.system('') # 윈도우 터미널 색상 활성화
    
    print("\n" + "="*85)
    print(" [최종 분석 보고: 31일간의 알고리즘 성과 스코어보드] ")
    print("="*85)
    
    # 1. 데이터 계산
    kpi = res_df.groupby(['Slotting', 'Dispatching'])[['Distance', 'Time']].mean().round(1)
    base_dist = kpi.loc[('random', 'nearest'), 'Distance']
    kpi['성능개선율(%)'] = ((base_dist - kpi['Distance']) / base_dist * 100).round(1)
    kpi = kpi.sort_values(by='Distance')

    # 색상 코드 정의
    YELLOW = '\033[93m' # 밝은 노란색 글자
    RESET = '\033[0m'   # 색상 초기화

    # 2. 결과 출력 (1등만 색상 적용)
    # 헤더 출력
    print(f"{'Slotting':<15} {'Dispatching':<15} {'Distance':>12} {'Time':>12} {'성능개선율(%)':>15}")
    print("-" * 85)

    for i, ((slot, disp), row) in enumerate(kpi.iterrows()):
        # 한 줄 문자열 생성
        line = f"{slot:<15} {disp:<15} {row['Distance']:>12.1f} {row['Time']:>12.1f} {row['성능개선율(%)']:>14}%"
        
        if i == 0: # 1등일 때만 노란색으로 출력
            print(f"{YELLOW}{line} ★ 최적{RESET}")
        else:
            print(line)
    
    print("-" * 85)
    best = kpi.index[0]
    print(f"\n★ 최적 조합: {YELLOW}[{best[0]} x {best[1]}]{RESET}")
    print(f"   => 베이스라인 대비 {YELLOW}{kpi.loc[best, '성능개선율(%)']}%{RESET} 효율 향상 증명")


# ==========================================
# main 함수 (수정)
# ==========================================
def main():
    # 1. 데이터 파일 읽기
    data_dir = Path(__file__).resolve().parent.parent / 'data'
    outbound_plan = pd.read_csv(data_dir / 'outbound_plan_toy.csv')
    production_plan = pd.read_csv(data_dir / 'production_plan_toy.csv')
    bom_df = pd.read_excel(data_dir / 'BOM_toy.xlsx')

    outbound_plan['생산일자'] = pd.to_datetime(outbound_plan['생산일자']).dt.date
    production_plan['생산일자'] = pd.to_datetime(production_plan['생산일자']).dt.date
    unique_dates = sorted(outbound_plan['생산일자'].unique())
    part_list = outbound_plan['부품코드'].unique().tolist() 
    
    # 확장된 3블록(통로 18개) 레이아웃에서 슬로팅 4종 × 디스패칭 3종 조합 (총 12개 조합)을 검증합니다.
    # (강화학습(RL) 및 GA 기반 배정/배치는 이 버전에서 완전히 제외되었습니다.)
    slotting_rules = ['random', 'abc', 'characteristic', 'kitting']
    dispatch_rules = ['nearest', 'density', 'load_balance']

    all_results_summary = []
    move_logs_all = []
    dispatch_logs_all = []

    total_combos = len(slotting_rules) * len(dispatch_rules)
    print(f"총 {len(unique_dates)}일치 데이터에 대해 시뮬레이션을 시작합니다. (총 {total_combos}개 조합)")

    # 4. 시뮬레이션 실행 루프 (슬로팅 x 디스패칭)
    for s_rule in slotting_rules:
        for d_rule in dispatch_rules:
            inventory = None 
            for i, date in enumerate(unique_dates):
                # [핵심] 7일마다 재배치
                if i % 7 == 0:
                    upcoming_7 = unique_dates[i : i+7]
                    weekly_plan = production_plan[production_plan['생산일자'].isin(upcoming_7)]
                    inventory = generate_fixed_inventory(s_rule, part_list, weekly_plan, bom_df, outbound_plan, seed=100)
                
                    print(f">> {date} [{s_rule} x {d_rule}] 실행 중...")

                date_plan = outbound_plan[outbound_plan['생산일자'] == date]
                res = run_simulation(date, inventory, d_rule, date_plan)

                # 결과 수집
                res['Slotting'] = s_rule
                res['Dispatching'] = d_rule
                all_results_summary.append(res)

                move_tmp = res['Move_log'].copy()
                move_tmp['Slotting'], move_tmp['Dispatching'], move_tmp['Date'] = s_rule, d_rule, date
                move_logs_all.append(move_tmp)

                disp_tmp = res['Dispatch_log'].copy()
                disp_tmp['Slotting'], disp_tmp['Dispatching'], disp_tmp['Date'] = s_rule, d_rule, date
                dispatch_logs_all.append(disp_tmp)

    # 5. 최종 분석 및 모든 조합 시각화
    df_res = pd.DataFrame(all_results_summary)
    move_all_df = pd.concat(move_logs_all, ignore_index=True)
    
    # (1) 주 단위 히트맵 시각화 표 (16개 조합) 출력 함수 호출
    # 텍스트 성적표 출력 함수 호출
    report_weekly_scoreboard(df_res)
    report_kpi_scoreboard(df_res)       
    
    print("\n" + "="*60)
    print(f" 모든 조합({total_combos}개)에 대한 상세 리포트 그래프를 생성합니다.")
    print(f" ※ 주의: 총 {total_combos}개의 조합 시각화 창이 순차적으로 열립니다.")
    print("="*60)

    # (2) 12개 조합에 대한 개별 시각화 진행
    for s_rule in slotting_rules:
        for d_rule in dispatch_rules:
            visualize_rule_combination(move_all_df, s_rule, d_rule)

if __name__ == "__main__":
    main()
