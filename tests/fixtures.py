"""Synthetic contract fixtures, NEVER evidence of real LPL coverage."""
def game(mid="TEST_000001", minutes=30):
    participants = [dict(participantId=i, puuid=f"synthetic-player-{i}", teamId=100 if i<=5 else 200,
                         championId=i, championName=f"TestChampion{i}", riotIdGameName=f"TestPlayer{i}",
                         teamPosition="TEST", kills=0, deaths=0, assists=0) for i in range(1,11)]
    match = dict(metadata={"matchId": mid}, info=dict(gameVersion="test.1.1", gameDuration=minutes*60,
                 participants=participants, teams=[dict(teamId=100,win=True),dict(teamId=200,win=False)]))
    frames = []
    for minute in range(minutes+1):
        frames.append(dict(timestamp=minute*60000, events=[], participantFrames={str(i):dict(
            participantId=i, totalGold=500+minute*(300 if i<=5 else 280), currentGold=500,
            xp=minute*100, level=1+minute//2, minionsKilled=minute*5, jungleMinionsKilled=0,
            position={"x":500+i*100,"y":500+i*100}) for i in range(1,11)}))
    timeline=dict(metadata={"matchId":mid}, info=dict(frameInterval=60000, participants=[dict(participantId=i,puuid=f"synthetic-player-{i}") for i in range(1,11)],frames=frames))
    context=dict(gameId=f"test:{mid}",kind="competitive",synthetic=True,league="LPL",season="test-season",
                 seriesId=f"test-series:{mid}",gameNumber=1,startTime="2026-01-01T00:00:00Z",status="completed",
                 mappingEvidence="SYNTHETIC ONLY",teams={"100":{"id":"test-blue","name":"Test Blue"},"200":{"id":"test-red","name":"Test Red"}})
    return match,timeline,context
