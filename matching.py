# matching.py
import math
from datetime import datetime
from models import Donor
import re

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

def canonical_blood(bg):
    if not bg: return None
    s = str(bg).upper().strip()
    s = re.sub(r'\s+', '', s)
    s = s.replace('POS', '+').replace('NEG', '-').replace('+VE', '+').replace('-VE', '-')
    s = s.replace('OPOS', 'O+').replace('ONEG', 'O-')
    return s

def haversine_distance(lat1, lon1, lat2, lon2):
    try:
        R = 6371.0
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * \
            math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    except Exception:
        return float('inf')

def is_eligible(donor, min_days=90, min_weight=50.0, min_age=18, max_age=65):
    if not donor.is_available:
        return False
    if donor.weight_kg is None or donor.weight_kg < min_weight:
        return False
    donor_age = donor.age
    if donor_age is None and donor.dob:
        try:
            from datetime import date
            today = date.today()
            donor_age = today.year - donor.dob.year - ((today.month, today.day) < (donor.dob.month, donor.dob.day))
        except Exception:
            donor_age = None
    if donor_age is None or donor_age < min_age or donor_age > max_age:
        return False
    if donor.last_donation_date:
        days = (datetime.utcnow().date() - donor.last_donation_date).days
        if days < min_days:
            return False
    return True

def find_best_donors(required_group, req_lat, req_lon, max_results=10, max_distance_km=60):
    req_canon = canonical_blood(required_group)
    if not req_canon:
        return []
    compatible_groups = COMPATIBILITY.get(req_canon, [req_canon])

    all_donors = Donor.query.all()
    candidates = []
    for d in all_donors:
        try:
            dbg = canonical_blood(d.blood_group)
        except Exception:
            dbg = None
        if dbg not in compatible_groups:
            continue
        if not is_eligible(d):
            continue
        if d.latitude is None or d.longitude is None:
            continue
        dist = haversine_distance(req_lat, req_lon, d.latitude, d.longitude)
        if dist > max_distance_km:
            continue
        score = 1.0 / (dist + 0.1)
        if d.is_online:
            score *= 1.3
        candidates.append((d, dist, score))
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[:max_results]
