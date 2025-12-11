# matching.py (updated)
import math
from datetime import datetime, timedelta
from models import Donor
import re

# canonical mapping (upper-case canonical keys)
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
    # map common variants to standard e.g. "O +VE" -> "O+"
    s = re.sub(r'\s+', '', s)  # remove spaces
    s = s.replace('POS', '+').replace('NEG', '-').replace('+VE', '+').replace('-VE', '-')
    # normalize common textual variants
    s = s.replace('OPOS', 'O+').replace('ONEG', 'O-')
    # If already like "O+" or "A-" it's fine
    return s

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
    # normalize requested group
    req_canon = canonical_blood(required_group)
    if not req_canon:
        return []

    compatible_groups = COMPATIBILITY.get(req_canon, [])
    # also try to include if requested group already something else
    if not compatible_groups:
        compatible_groups = [req_canon]

    # query donors by compatibility - use filter in Python to avoid DB format mismatch
    all_donors = Donor.query.all()
    donors = []
    for d in all_donors:
        dbg = canonical_blood(d.blood_group)
        if dbg in compatible_groups:
            donors.append(d)

    ranked = []
    for d in donors:
        if not is_eligible(d):
            continue
        # guard missing coords
        if d.latitude is None or d.longitude is None:
            continue
        dist = haversine_distance(req_lat, req_lon, d.latitude, d.longitude)
        score = 1 / (dist + 0.1)  # closer => higher
        ranked.append((d, dist, score))

    ranked.sort(key=lambda x: x[2], reverse=True)
    return ranked[:max_results]
