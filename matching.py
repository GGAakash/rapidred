import math
from datetime import datetime, timedelta
from models import Donor

COMPATIBILITY={
"O-":["O-"],
"O+":["O-","O+"],
"A-":["O-","A-"],
"A+":["O-","O+","A-","A+"],
"B-":["O-","B-"],
"B+":["O-","O+","B-","B+"],
"AB-":["O-","A-","B-","AB-"],
"AB+":["O-","O+","A-","A+","B-","B+","AB-","AB+"]
}

def haversine_distance(lat1,lon1,lat2,lon2):
    R=6371
    dlat=math.radians(lat2-lat1)
    dlon=math.radians(lon2-lon1)
    a=math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R*2*math.atan2(math.sqrt(a), math.sqrt(1-a))

def is_eligible(d):
    if not d.is_available: return False
    if d.last_donation_date:
        if datetime.utcnow().date() < d.last_donation_date + timedelta(days=90):
            return False
    return True

def find_best_donors(group,lat,lon,max_results=10):
    comp=COMPATIBILITY.get(group,[])
    donors=Donor.query.filter(Donor.blood_group.in_(comp)).all()
    ranked=[]
    for d in donors:
        if not is_eligible(d): continue
        dist=haversine_distance(lat,lon,d.latitude,d.longitude)
        score=1/(dist+0.1)
        ranked.append((d,dist,score))
    ranked.sort(key=lambda x:x[2], reverse=True)
    return ranked[:max_results]
