import math
from datetime import datetime, timedelta
from models import Donor

COMPATIBILITY = {
    "O-": ["O-"],
    "O+": ["O-", "O+"],
    "A-": ["O-", "A-"],
    "A+": ["O-", "O+", "A-", "A+"],
    "B-": ["O-", "B-"],
    "B+": ["O-", "O+", "B-", "B+"],
    "AB-": ["O-", "A-", "B-", "AB-"],
    "AB+": ["O-", "O+", "A-", "A+", "B-", "B+", "AB-", "AB+"],
}

def haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * \
        math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def is_eligible(donor):
    if not donor.is_available:
        return False
    if donor.last_donation_date:
        min_gap = donor.last_donation_date + timedelta(days=90)
        if datetime.utcnow().date() < min_gap:
            return False
    return True

def find_best_donors(required_group, req_lat, req_lon, max_results=10):
    compatible_groups = COMPATIBILITY.get(required_group, [])
    donors = Donor.query.filter(Donor.blood_group.in_(compatible_groups)).all()

    ranked = []
    for d in donors:
        if not is_eligible(d):
            continue
        dist = haversine_distance(req_lat, req_lon, d.latitude, d.longitude)
        score = 1 / (dist + 0.1)  # closer => higher
        ranked.append((d, dist, score))

    ranked.sort(key=lambda x: x[2], reverse=True)
    return ranked[:max_results]
