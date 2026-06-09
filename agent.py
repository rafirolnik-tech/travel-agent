"""
סוכן מחירי נסיעות — Travel Price Agent
מחפש מחירי מלונות וטיסות ממדינות שונות ומוצא את הזול ביותר
"""

from flask import Flask, request, jsonify, send_from_directory, Response
from flask_cors import CORS
import requests
import json
import traceback
import os

app = Flask(__name__, static_folder='.')
CORS(app)

# Server-side SerpAPI key (optional — set SERP_API_KEY env var on Render)
SERVER_API_KEY = os.environ.get('SERP_API_KEY', '')

def _resolve_key(client_key):
    """Use client key if provided, otherwise fall back to server key.
    'SERVER' is a frontend sentinel meaning 'use server key'."""
    k = (client_key or '').strip()
    if not k or k == 'SERVER':
        return SERVER_API_KEY
    return k

@app.route('/config')
def get_config():
    """Tell the frontend whether a server-side API key is configured."""
    return jsonify({'has_server_key': bool(SERVER_API_KEY)})

# מדינות לבדיקת geo-pricing
GEO_COUNTRIES = {
    '🇮🇱 ישראל':    {'gl': 'il', 'hl': 'en'},
    '🇹🇭 תאילנד':   {'gl': 'th', 'hl': 'en'},
    '🇮🇳 הודו':     {'gl': 'in', 'hl': 'en'},
    '🇸🇬 סינגפור':  {'gl': 'sg', 'hl': 'en'},
    '🇺🇸 ארה"ב':    {'gl': 'us', 'hl': 'en'},
    '🇬🇧 בריטניה':  {'gl': 'gb', 'hl': 'en'},
    '🇩🇪 גרמניה':   {'gl': 'de', 'hl': 'en'},
    '🇦🇺 אוסטרליה': {'gl': 'au', 'hl': 'en'},
    '🇯🇵 יפן':      {'gl': 'jp', 'hl': 'en'},
    '🇰🇷 קוריאה':   {'gl': 'kr', 'hl': 'en'},
}


def parse_price(price_str):
    """Convert price string to float"""
    if not price_str:
        return float('inf')
    try:
        return float(str(price_str).replace('$', '').replace(',', '').replace('₪', '').strip())
    except:
        return float('inf')


import re as _re
from datetime import datetime as _dt

def _extract_stars(p):
    """Extract star count from hotel_class field (e.g. '4-star hotel' → 4).
    Returns None when rating is unknown (no hotel_class), int otherwise."""
    hc = p.get('hotel_class', '')
    if hc:
        m = _re.search(r'(\d+)', hc)
        if m:
            return int(m.group(1))
    return None  # unknown — do NOT treat as 0-star

def _calc_nights(check_in, check_out):
    try:
        return max((_dt.strptime(check_out,'%Y-%m-%d') - _dt.strptime(check_in,'%Y-%m-%d')).days, 1)
    except:
        return 1


def search_hotel_single(api_key, query, check_in, check_out, adults,
                         children_ages, min_stars, max_budget_usd, country_name, rooms=1):
    """Search hotels for a single country — returns ALL matching hotels"""
    if country_name not in GEO_COUNTRIES:
        return {'country': country_name, 'status': 'error', 'error': 'מדינה לא ידועה'}

    geo = GEO_COUNTRIES[country_name]
    try:
        effective_adults = adults
        effective_children = []
        for age in children_ages:
            if int(age) >= 16:
                effective_adults += 1
            else:
                effective_children.append(age)

        params = {
            'engine':         'google_hotels',
            'q':              query,
            'check_in_date':  check_in,
            'check_out_date': check_out,
            'adults':         effective_adults,
            'rooms':          max(1, int(rooms)),
            'gl':             geo['gl'],
            'hl':             geo['hl'],
            'currency':       'USD',
            'api_key':        api_key,
        }
        if effective_children:
            params['children'] = len(effective_children)
            params['children_ages'] = ','.join(str(int(a)) for a in effective_children)

        print(f"[{country_name}] guests: {effective_adults} adults + {len(effective_children)} children {effective_children} | q={query!r}")
        resp = requests.get('https://serpapi.com/search', params=params, timeout=25)

        print(f"  ↳ status={resp.status_code} | gl={geo['gl']}")
        if resp.status_code == 401:
            return {'country': country_name, 'status': 'error', 'error': 'API Key שגוי'}
        if resp.status_code != 200:
            try:
                err_msg = resp.json().get('error', f'HTTP {resp.status_code}')
            except Exception:
                err_msg = f'HTTP {resp.status_code}'
            return {'country': country_name, 'status': 'error', 'error': err_msg}

        data = resp.json()
        if 'error' in data:
            return {'country': country_name, 'status': 'error', 'error': data['error']}

        properties = data.get('properties', [])
        print(f"  ↳ {len(properties)} properties | hotel_class sample: {[p.get('hotel_class','') for p in properties[:3]]}")

        if not properties:
            return {'country': country_name, 'status': 'no_results', 'message': 'לא נמצאו מלונות ביעד זה'}

        nights = _calc_nights(check_in, check_out)
        results = []
        near_budget = []  # hotels within 10% over budget (fallback)

        for p in properties:
            stars = _extract_stars(p)
            # Star filter strategy:
            # - min_stars >= 4: strict — require known star rating (None = unclassified hostel/guesthouse)
            # - min_stars = 3:  soft  — allow unrated hotels (may be boutique/quality unlabeled)
            # - min_stars = 0:  none  — show all
            if min_stars >= 4:
                if stars is None or stars < min_stars:
                    continue  # strict: unrated hotels excluded for 4★/5★ searches
            elif min_stars == 3:
                if stars is not None and stars < min_stars:
                    continue  # soft: only exclude if KNOWN to be below minimum

            price = parse_price(p.get('rate_per_night', {}).get('lowest'))
            if price == float('inf'):
                continue  # no price data

            images = p.get('images', [])
            thumb  = images[0].get('thumbnail', '') if images else ''

            # Extract OTA links captured at the search geo (Indian/Singapore IP)
            agoda_link = booking_link = trip_link = ''
            cheapest_link = ''
            cheapest_source = ''
            cheapest_price_num = float('inf')

            for pr in p.get('prices', []):
                src = pr.get('source', '').lower()
                lnk = pr.get('link', '')
                if not lnk:
                    continue
                pr_price = parse_price(
                    pr.get('rate_per_night', {}).get('extracted_lowest') or
                    pr.get('rate_per_night', {}).get('lowest', '')
                )
                # Track cheapest across ALL OTAs
                if pr_price < cheapest_price_num:
                    cheapest_price_num = pr_price
                    cheapest_link = lnk
                    cheapest_source = pr.get('source', '')
                # Also keep named links for compare row
                if 'agoda' in src and not agoda_link:
                    agoda_link = lnk
                elif 'booking' in src and not booking_link:
                    booking_link = lnk
                elif 'trip' in src and not trip_link:
                    trip_link = lnk

            hotel_entry = {
                'country':          country_name,
                'status':           'found',
                'hotel_name':       p.get('name', ''),
                'price_num':        price,
                'price_per_night':  f'${price:.0f}',
                'total_price':      f'${price * nights:.0f}',
                'stars':            stars if stars is not None else '',
                'hotel_class':      p.get('hotel_class', ''),
                'rating':           p.get('overall_rating', ''),
                'reviews':          p.get('reviews', ''),
                'link':             p.get('link', '#'),
                'cheapest_link':    cheapest_link,
                'cheapest_source':  cheapest_source,
                'agoda_link':       agoda_link,
                'booking_link':     booking_link,
                'trip_link':        trip_link,
                'thumbnail':        thumb,
                'nights':           nights,
            }

            if max_budget_usd > 0 and price > max_budget_usd:
                # Within 10% over budget → keep as near-budget fallback
                if price <= max_budget_usd * 1.10:
                    near_budget.append(hotel_entry)
                continue  # don't add to main results

            results.append(hotel_entry)

        print(f"  ↳ {len(results)} in-budget | {len(near_budget)} near-budget (≤10% over) | budget=${max_budget_usd}")

        if not results and not near_budget:
            msg = f'לא נמצא מלון {min_stars}★+ ביעד זה' if min_stars > 0 else 'לא נמצאו מלונות ביעד זה'
            return {'country': country_name, 'status': 'no_results', 'message': msg}

        return {'country': country_name, 'status': 'found_many', 'hotels': results, 'near_budget': near_budget}

    except requests.Timeout:
        return {'country': country_name, 'status': 'error', 'error': 'timeout — נסה שוב'}
    except Exception as e:
        traceback.print_exc()
        return {'country': country_name, 'status': 'error', 'error': str(e)}


def search_flight_single(api_key, origin, destination, departure_date,
                          return_date, adults, country_name):
    """Search flights for a single country"""
    if country_name not in GEO_COUNTRIES:
        return {'country': country_name, 'status': 'error', 'error': 'מדינה לא ידועה'}

    geo = GEO_COUNTRIES[country_name]
    try:
        params = {
            'engine':         'google_flights',
            'departure_id':   origin.upper(),
            'arrival_id':     destination.upper(),
            'outbound_date':  departure_date,
            'gl':             geo['gl'],
            'hl':             geo['hl'],
            'currency':       'USD',
            'adults':         adults,
            'api_key':        api_key,
        }
        if return_date:
            params['return_date'] = return_date
            params['type'] = '1'  # round trip
        else:
            params['type'] = '2'  # one way

        resp = requests.get('https://serpapi.com/search', params=params, timeout=25)

        if resp.status_code == 401:
            return {'country': country_name, 'status': 'error', 'error': 'API Key שגוי'}
        if resp.status_code != 200:
            try:
                err_body = resp.json()
                err_msg = err_body.get('error', f'HTTP {resp.status_code}')
            except Exception:
                err_msg = f'HTTP {resp.status_code}'
            return {'country': country_name, 'status': 'error', 'error': err_msg}

        data = resp.json()
        if 'error' in data:
            return {'country': country_name, 'status': 'error', 'error': data['error']}

        all_flights = data.get('best_flights', []) + data.get('other_flights', [])
        if not all_flights:
            return {'country': country_name, 'status': 'no_results', 'message': 'לא נמצאו טיסות'}

        cheapest = min(all_flights, key=lambda f: f.get('price', 999999))
        price    = cheapest.get('price', 0)
        flights  = cheapest.get('flights', [{}])
        airline  = flights[0].get('airline', '') if flights else ''
        stops    = len(flights) - 1

        return {
            'country':    country_name,
            'status':     'found',
            'price':      f"${price}",
            'price_num':  price,
            'airline':    airline,
            'stops':      stops,
            'stops_label': 'ישיר' if stops == 0 else f'{stops} עצירות',
            'duration':   cheapest.get('total_duration', ''),
            'link':       'https://www.google.com/flights',
        }

    except requests.Timeout:
        return {'country': country_name, 'status': 'error', 'error': 'timeout'}
    except Exception as e:
        return {'country': country_name, 'status': 'error', 'error': str(e)}


# ── Routes ────────────────────────────────────────────────────

@app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')


COUNTRIES_BY_REGION = {
    'asia': [
        {'he': 'אינדונזיה',    'en': 'Indonesia'},
        {'he': 'ויאטנם',       'en': 'Vietnam'},
        {'he': 'טאיוואן',      'en': 'Taiwan'},
        {'he': 'טורקיה',       'en': 'Turkey'},
        {'he': 'יפן',          'en': 'Japan'},
        {'he': 'ירדן',         'en': 'Jordan'},
        {'he': 'כוויית',       'en': 'Kuwait'},
        {'he': 'מלזיה',        'en': 'Malaysia'},
        {'he': 'נפאל',         'en': 'Nepal'},
        {'he': 'סין',          'en': 'China'},
        {'he': 'סינגפור',      'en': 'Singapore'},
        {'he': 'סרי לנקה',     'en': 'Sri Lanka'},
        {'he': 'עומאן',        'en': 'Oman'},
        {'he': 'פיליפינים',    'en': 'Philippines'},
        {'he': 'קמבודיה',      'en': 'Cambodia'},
        {'he': 'קוריאה',       'en': 'South Korea'},
        {'he': 'קטר',          'en': 'Qatar'},
        {'he': 'תאילנד',       'en': 'Thailand'},
        {'he': 'הודו',         'en': 'India'},
        {'he': 'איחוד האמירויות', 'en': 'United Arab Emirates'},
        {'he': 'בחריין',       'en': 'Bahrain'},
        {'he': 'ישראל',        'en': 'Israel'},
        {'he': 'לאוס',         'en': 'Laos'},
        {'he': 'מיאנמר',       'en': 'Myanmar'},
    ],
    'europe': [
        {'he': 'אוסטריה',      'en': 'Austria'},
        {'he': 'איטליה',       'en': 'Italy'},
        {'he': 'איסלנד',       'en': 'Iceland'},
        {'he': 'אירלנד',       'en': 'Ireland'},
        {'he': 'בלגיה',        'en': 'Belgium'},
        {'he': 'בריטניה',      'en': 'United Kingdom'},
        {'he': 'גרמניה',       'en': 'Germany'},
        {'he': 'דנמרק',        'en': 'Denmark'},
        {'he': 'הולנד',        'en': 'Netherlands'},
        {'he': 'הונגריה',      'en': 'Hungary'},
        {'he': 'יוון',         'en': 'Greece'},
        {'he': 'נורבגיה',      'en': 'Norway'},
        {'he': 'ספרד',         'en': 'Spain'},
        {'he': 'פולין',        'en': 'Poland'},
        {'he': 'פורטוגל',      'en': 'Portugal'},
        {'he': 'צ\'כיה',       'en': 'Czech Republic'},
        {'he': 'צרפת',         'en': 'France'},
        {'he': 'רומניה',       'en': 'Romania'},
        {'he': 'שוודיה',       'en': 'Sweden'},
        {'he': 'שווייץ',       'en': 'Switzerland'},
        {'he': 'קרואטיה',      'en': 'Croatia'},
        {'he': 'פינלנד',       'en': 'Finland'},
    ],
    'americas-north': [
        {'he': 'ארה"ב',        'en': 'United States'},
        {'he': 'קנדה',         'en': 'Canada'},
        {'he': 'מקסיקו',       'en': 'Mexico'},
        {'he': 'קובה',         'en': 'Cuba'},
        {'he': 'קוסטה ריקה',   'en': 'Costa Rica'},
        {'he': 'פנמה',         'en': 'Panama'},
        {'he': 'גואטמלה',      'en': 'Guatemala'},
        {'he': 'הונדורס',      'en': 'Honduras'},
        {'he': 'ג\'מייקה',     'en': 'Jamaica'},
        {'he': 'הרפובליקה הדומיניקנית', 'en': 'Dominican Republic'},
    ],
    'americas-south': [
        {'he': 'ארגנטינה',     'en': 'Argentina'},
        {'he': 'ברזיל',        'en': 'Brazil'},
        {'he': 'בוליביה',      'en': 'Bolivia'},
        {'he': 'וונצואלה',     'en': 'Venezuela'},
        {'he': 'אקוודור',      'en': 'Ecuador'},
        {'he': 'קולומביה',     'en': 'Colombia'},
        {'he': 'צ\'ילה',       'en': 'Chile'},
        {'he': 'פרו',          'en': 'Peru'},
        {'he': 'אורוגוואי',    'en': 'Uruguay'},
        {'he': 'פרגוואי',      'en': 'Paraguay'},
    ],
    'africa': [
        {'he': 'אתיופיה',      'en': 'Ethiopia'},
        {'he': 'גאנה',         'en': 'Ghana'},
        {'he': 'טנזניה',       'en': 'Tanzania'},
        {'he': 'טוניסיה',      'en': 'Tunisia'},
        {'he': 'מרוקו',        'en': 'Morocco'},
        {'he': 'מצרים',        'en': 'Egypt'},
        {'he': 'ניגריה',       'en': 'Nigeria'},
        {'he': 'סנגל',         'en': 'Senegal'},
        {'he': 'קניה',         'en': 'Kenya'},
        {'he': 'דרום אפריקה',  'en': 'South Africa'},
        {'he': 'זימבבואה',     'en': 'Zimbabwe'},
        {'he': 'אוגנדה',       'en': 'Uganda'},
        {'he': 'רואנדה',       'en': 'Rwanda'},
    ],
    'oceania': [
        {'he': 'אוסטרליה',     'en': 'Australia'},
        {'he': 'ניו זילנד',    'en': 'New Zealand'},
        {'he': 'פיג\'י',       'en': 'Fiji'},
        {'he': 'פפואה גינאה החדשה', 'en': 'Papua New Guinea'},
        {'he': 'סמואה',        'en': 'Samoa'},
        {'he': 'וונואטו',      'en': 'Vanuatu'},
    ],
}

CITIES_BY_COUNTRY = {
    'Thailand':       [('בנגקוק','Bangkok'),('פוקט','Phuket'),("צ'יאנג מאי",'Chiang Mai'),('פטאיה','Pattaya'),('קו סמוי','Koh Samui'),('קראבי','Krabi'),('הואה הין','Hua Hin'),('איוטאיה','Ayutthaya')],
    'Japan':          [('טוקיו','Tokyo'),('אוסקה','Osaka'),('קיוטו','Kyoto'),('סאפורו','Sapporo'),('פוקואוקה','Fukuoka'),('הירושימה','Hiroshima'),('נארה','Nara'),('יוקוהמה','Yokohama')],
    'India':          [('מומבאי','Mumbai'),('דלהי','Delhi'),('גואה','Goa'),("ג'איפור",'Jaipur'),('בנגלור','Bangalore'),("צ'נאי",'Chennai'),('קולקטה','Kolkata'),('אגרה','Agra')],
    'Indonesia':      [('באלי','Bali'),("ג'קרטה",'Jakarta'),('יוגיאקרטה','Yogyakarta'),('לומבוק','Lombok'),('סורבאיה','Surabaya'),('מדן','Medan'),('בנדונג','Bandung')],
    'Vietnam':        [('האנוי','Hanoi'),('הו צ\'י מין','Ho Chi Minh City'),('דה נאנג','Da Nang'),('הוי אן','Hoi An'),('ניה טראנג','Nha Trang'),('הואה','Hue'),('פו קוק','Phu Quoc')],
    'Singapore':      [('סינגפור','Singapore')],
    'Malaysia':       [('קואלה לומפור','Kuala Lumpur'),('פנאנג','Penang'),('לנגקאווי','Langkawi'),('קוטה קינבאלו','Kota Kinabalu'),("ג'והור בהרו",'Johor Bahru'),('מלאקה','Malacca')],
    'Philippines':    [('מנילה','Manila'),('סבו','Cebu'),('בוראקאי','Boracay'),('פלאוואן','Palawan'),('דאבאו','Davao'),('בוהול','Bohol')],
    'South Korea':    [('סיאול','Seoul'),('בוסאן','Busan'),("ג'ג'ו",'Jeju'),('אינצ\'ון','Incheon'),('ג\'יאונג\'ו','Gyeongju'),('דאגו','Daegu')],
    'China':          [('בייג\'ינג','Beijing'),('שנגחאי','Shanghai'),('גואנגז\'ו','Guangzhou'),('שנז\'ן','Shenzhen'),("צ'נגדו",'Chengdu'),("צ'ונגצ'ינג",'Chongqing'),("שי'אן",'Xian'),('האנגז\'ו','Hangzhou')],
    'Taiwan':         [('טאיפיי','Taipei'),('קאושיונג','Kaohsiung'),('טאינאן','Tainan'),('טאיצ\'ונג','Taichung')],
    'Cambodia':       [('סיאם ריפ','Siem Reap'),('פנום פן','Phnom Penh'),('סיהנוקוויל','Sihanoukville')],
    'Nepal':          [('קטמנדו','Kathmandu'),('פוקרה','Pokhara'),('צ\'יטוואן','Chitwan')],
    'Sri Lanka':      [('קולומבו','Colombo'),('קנדי','Kandy'),('גאלה','Galle'),('נגומבו','Negombo'),('אלה','Ella')],
    'Turkey':         [('איסטנבול','Istanbul'),('אנטליה','Antalya'),('קפדוקיה','Cappadocia'),('אנקרה','Ankara'),('בודרום','Bodrum'),('איזמיר','Izmir')],
    'United Arab Emirates': [('דובאי','Dubai'),('אבו דאבי','Abu Dhabi'),('שארג\'ה','Sharjah'),('עג\'מן','Ajman')],
    'Qatar':          [('דוחה','Doha')],
    'Jordan':         [('עמאן','Amman'),('פטרה','Petra'),('עקבה','Aqaba'),('ואדי רם','Wadi Rum')],
    'Israel':         [('תל אביב','Tel Aviv'),('ירושלים','Jerusalem'),('אילת','Eilat'),('חיפה','Haifa'),('נצרת','Nazareth')],
    'Kuwait':         [('כווית סיטי','Kuwait City')],
    'Bahrain':        [('מנאמה','Manama')],
    'Oman':           [('מסקט','Muscat'),('סלאלה','Salalah'),('ניזווה','Nizwa')],
    'Laos':           [('לואנג פראבנג','Luang Prabang'),('וינטיאן','Vientiane'),('וואנג וינג','Vang Vieng')],
    'Myanmar':        [('יאנגון','Yangon'),('מנדלי','Mandalay'),('בגאן','Bagan'),("אינלה לייק",'Inle Lake')],
    'United Kingdom': [('לונדון','London'),('אדינבורו','Edinburgh'),('מנצ\'סטר','Manchester'),('בירמינגהם','Birmingham'),('ליברפול','Liverpool'),('בריסטול','Bristol'),('אוקספורד','Oxford'),('קיימברידג\'','Cambridge')],
    'France':         [('פריז','Paris'),('ניס','Nice'),('ליון','Lyon'),('מרסיי','Marseille'),('בורדו','Bordeaux'),('טולוז','Toulouse'),('סטרסבורג','Strasbourg')],
    'Spain':          [('ברצלונה','Barcelona'),('מדריד','Madrid'),('סביליה','Seville'),('ולנסיה','Valencia'),('מיורקה','Mallorca'),('איביזה','Ibiza'),('גרנדה','Granada')],
    'Italy':          [('רומא','Rome'),('מילאנו','Milan'),('ונציה','Venice'),('פירנצה','Florence'),('נאפולי','Naples'),('אמלפי','Amalfi'),('צ\'ינקווה טרה','Cinque Terre'),('סיציליה','Sicily')],
    'Germany':        [('ברלין','Berlin'),('מינכן','Munich'),('המבורג','Hamburg'),('פרנקפורט','Frankfurt'),('קלן','Cologne'),('דרזדן','Dresden'),('היידלברג','Heidelberg')],
    'Netherlands':    [('אמסטרדם','Amsterdam'),('רוטרדם','Rotterdam'),('האג','The Hague'),("אוטרכט",'Utrecht')],
    'Greece':         [('אתונה','Athens'),('סנטוריני','Santorini'),('מיקונוס','Mykonos'),('כרתים','Crete'),('רודוס','Rhodes'),('קורפו','Corfu'),('סלוניקי','Thessaloniki')],
    'Portugal':       [('ליסבון','Lisbon'),('פורטו','Porto'),('אלגארבה','Algarve'),('מדיירה','Madeira'),('האיים האזוריים','Azores')],
    'Austria':        [('וינה','Vienna'),('זלצבורג','Salzburg'),('אינסברוק','Innsbruck'),('הלשטט','Hallstatt')],
    'Switzerland':    [('ציריך','Zurich'),('ג\'נבה','Geneva'),('ברן','Bern'),('אינטרלאקן','Interlaken'),('לוצרן','Lucerne'),('צרמט','Zermatt')],
    'Czech Republic': [('פראג','Prague'),('ברנו','Brno'),("צ'סקי קרומלוב",'Cesky Krumlov')],
    'Hungary':        [('בודפשט','Budapest'),('דברצן','Debrecen')],
    'Poland':         [('ורשה','Warsaw'),('קרקוב','Krakow'),('גדנסק','Gdansk'),('ורוצלב','Wroclaw')],
    'Croatia':        [('דוברובניק','Dubrovnik'),('זאגרב','Zagreb'),('ספליט','Split'),('הוואר','Hvar'),('זאדר','Zadar')],
    'Romania':        [('בוקרשט','Bucharest'),('קלוז\'','Cluj-Napoca'),('ברשוב','Brasov'),('סיביו','Sibiu')],
    'Sweden':         [('סטוקהולם','Stockholm'),('גטבורג','Gothenburg'),('מלמו','Malmo')],
    'Norway':         [('אוסלו','Oslo'),('ברגן','Bergen'),("טרומסה",'Tromsø'),('סטוונגר','Stavanger')],
    'Denmark':        [('קופנהגן','Copenhagen'),("ארהוס",'Aarhus')],
    'Finland':        [('הלסינקי','Helsinki'),('רובניאמי','Rovaniemi'),('טמפרה','Tampere')],
    'Iceland':        [('רייקיאוויק','Reykjavik'),('אקורירי','Akureyri')],
    'Ireland':        [('דבלין','Dublin'),('קורק','Cork'),("גולוויי",'Galway'),('קילרני','Killarney')],
    'Belgium':        [('בריסל','Brussels'),('ברוז\'','Bruges'),('גנט','Ghent'),('אנטוורפן','Antwerp')],
    'United States':  [('ניו יורק','New York'),('לוס אנג\'לס','Los Angeles'),('מיאמי','Miami'),('לאס וגאס','Las Vegas'),('אורלנדו','Orlando'),('שיקגו','Chicago'),('סן פרנסיסקו','San Francisco'),('סיאטל','Seattle'),('הוואי','Hawaii'),('ניו אורלינס','New Orleans'),('וושינגטון','Washington DC'),('בוסטון','Boston')],
    'Canada':         [('טורונטו','Toronto'),('ונקובר','Vancouver'),('מונטריאול','Montreal'),('קוויבק סיטי','Quebec City'),('קלגרי','Calgary'),("אוטווה",'Ottawa'),('בנף','Banff')],
    'Mexico':         [('קנקון','Cancun'),('מקסיקו סיטי','Mexico City'),('לוס קאבוס','Los Cabos'),('פוארטו ויארטה','Puerto Vallarta'),('טולום','Tulum'),('פלאיה דל כרמן','Playa del Carmen'),("ואחאקה",'Oaxaca')],
    'Cuba':           [('הוואנה','Havana'),('ואראדרו','Varadero'),('טרינידד','Trinidad'),('סיאנפואגוס','Cienfuegos')],
    'Costa Rica':     [("סן חוזה",'San Jose'),('מנואל אנטוניו','Manuel Antonio'),('ארנל','Arenal'),('גואנקסטה','Guanacaste')],
    'Jamaica':        [('קינגסטון','Kingston'),('מונטגו ביי','Montego Bay'),('נגריל','Negril'),('אוצ\'ו ריוס','Ocho Rios')],
    'Dominican Republic': [('פונטה קאנה','Punta Cana'),('סנטו דומינגו','Santo Domingo'),('פוארטו פלטה','Puerto Plata'),('לה רומנה','La Romana')],
    'Panama':         [('פנמה סיטי','Panama City'),('בוקס דל טורו','Bocas del Toro'),('בוקטה','Boquete')],
    'Brazil':         [('ריו דה ז\'נרו','Rio de Janeiro'),('סאו פאולו','São Paulo'),('סלבדור','Salvador'),('פלוריאנופוליס','Florianopolis'),('פוז דו איגואסו','Foz do Iguaçu'),('מנאוס','Manaus'),('פורטלזה','Fortaleza')],
    'Argentina':      [('בואנוס איירס','Buenos Aires'),('ברילוצ\'ה','Bariloche'),('מנדוסה','Mendoza'),('סלטה','Salta'),('אושוואיה','Ushuaia'),('איגואסו','Iguazu')],
    'Chile':          [('סנטיאגו','Santiago'),('פטגוניה','Patagonia'),('אטקמה','Atacama'),('אי הפסחא','Easter Island'),('ולפאראיסו','Valparaiso')],
    'Peru':           [('לימה','Lima'),('קוסקו','Cusco'),('מאצ\'ו פיצ\'ו','Machu Picchu'),('ארקיפה','Arequipa'),("איקיטוס",'Iquitos')],
    'Colombia':       [('בוגוטה','Bogota'),('מדליין','Medellin'),('קרטחינה','Cartagena'),('קאלי','Cali'),('סנטה מרטה','Santa Marta')],
    'Ecuador':        [('קיטו','Quito'),('גלפגוס','Galapagos'),('קואנקה','Cuenca'),("באניוס",'Banos')],
    'Bolivia':        [('לה פאס','La Paz'),('אויוני','Uyuni'),('סוקרה','Sucre'),('סנטה קרוז','Santa Cruz')],
    'Uruguay':        [('מונטווידאו','Montevideo'),('פונטה דל אסטה','Punta del Este')],
    'Egypt':          [('קהיר','Cairo'),('לוקסור','Luxor'),('אסואן','Aswan'),('שארם א-שייח','Sharm el-Sheikh'),('הורגדה','Hurghada'),('אלכסנדריה','Alexandria')],
    'Morocco':        [('מרקש','Marrakech'),('קזבלנקה','Casablanca'),('פס','Fez'),('טנג\'יר','Tangier'),('שפשאון','Chefchaouen'),('אגאדיר','Agadir')],
    'Tunisia':        [('תוניס','Tunis'),("ד'רבה",'Djerba'),('סוסה','Sousse'),('המאמט','Hammamet')],
    'South Africa':   [('קייפטאון','Cape Town'),('יוהנסבורג','Johannesburg'),('דורבן','Durban'),('קרוגר','Kruger'),('סטלנבוש','Stellenbosch'),('גארדן רוט','Garden Route')],
    'Kenya':          [('נירובי','Nairobi'),('מומבסה','Mombasa'),('מאסאי מארה','Maasai Mara'),('אמבוסלי','Amboseli'),('דיאני','Diani')],
    'Tanzania':       [('דאר א-סלאם','Dar es Salaam'),('זנזיבר','Zanzibar'),('סרנגטי','Serengeti'),('ארושה','Arusha'),('קילימנג\'רו','Kilimanjaro')],
    'Ethiopia':       [('אדיס אבבה','Addis Ababa'),('לליבלה','Lalibela'),('גונדר','Gondar'),('אקסום','Axum')],
    'Ghana':          [('אקרה','Accra'),('קייפ קוסט','Cape Coast'),('קומאסי','Kumasi')],
    'Nigeria':        [('לגוס','Lagos'),('אבוג\'ה','Abuja')],
    'Rwanda':         [("קיגאלי",'Kigali'),('הרי הגורילות','Volcanoes')],
    'Uganda':         [("קמפלה",'Kampala'),('בווינדי','Bwindi'),('מלכת אליזבת','Queen Elizabeth')],
    'Zimbabwe':       [('הרארה','Harare'),('מפלי ויקטוריה','Victoria Falls'),('הוואנגה','Hwange')],
    'Senegal':        [('דקאר','Dakar'),('סנט לואי','Saint-Louis'),('קסמאנס','Casamance')],
    'Australia':      [('סידני','Sydney'),('מלבורן','Melbourne'),('בריסביין','Brisbane'),('פרת\'','Perth'),('קיירנס','Cairns'),('גולד קוסט','Gold Coast'),('אדלייד','Adelaide'),('דארווין','Darwin'),('הובארט','Hobart')],
    'New Zealand':    [('אוקלנד','Auckland'),("קווינסטאון",'Queenstown'),('וולינגטון','Wellington'),("כרייסטצ'רץ",'Christchurch'),('רוטורואה','Rotorua'),('מילפורד סאונד','Milford Sound')],
    'Fiji':           [('נאדי','Nadi'),('סובה','Suva'),('קורל קוסט','Coral Coast'),("איי ייסאוה",'Yasawa Islands')],
    'Papua New Guinea': [('פורט מורסבי','Port Moresby')],
    'Samoa':          [('אפיה','Apia')],
    'Vanuatu':        [('פורט וילה','Port Vila')],
}


NEIGHBORHOODS = {
    # אסיה
    'Phuket':             [('פאטונג — חיי לילה ושוקי קניות','Patong'),('קאטה — חוף משפחתי ושקט','Kata'),('קארון — חוף שקט ומרווח','Karon'),('קמאלה — יוקרה ושקט','Kamala'),('בנג טאו — מלונות בוטיק','Bang Tao'),('אולד טאון','Phuket Town')],
    'Bangkok':            [('סוקומוויט — מרכז תיירותי','Sukhumvit'),('סילום — עסקים ובר','Silom'),('ריוורסייד — נוף נהר','Riverside'),('העיר העתיקה','Old City'),("ארי — שכונת מקומיים",'Ari')],
    'Bali':               [('סמינייאק — מלונות בוטיק ומסעדות','Seminyak'),('קוטה — חוף עם גלים','Kuta'),('אובוד — טבע ותרבות','Ubud'),('נוסה דואה — ריזורטים יוקרתיים','Nusa Dua'),("ג'ימבאראן — דייגים וסאנסט",'Jimbaran'),('צ\'נגו — גלשנים ובוהמה','Canggu')],
    'Pattaya':            [('פאטאיה מרכז','Central Pattaya'),('ג\'ומטיין — חוף משפחתי','Jomtien'),('פאטאיה צפון','North Pattaya'),('נאקלואה — שקט','Naklua')],
    'Chiang Mai':         [('עיר עתיקה — מקדשים','Old City'),('נימן — ברים וקפה','Nimman'),('ריוורסייד','Riverside')],
    'Ho Chi Minh City':   [('מחוז 1 — מרכז','District 1'),('מחוז 3','District 3'),('ת\'או דיאן — יוקרה','Thao Dien')],
    'Hanoi':              [('העיר העתיקה','Old Quarter'),('הואן קיים — אגם','Hoan Kiem'),('בה דין','Ba Dinh')],
    'Tokyo':              [('שינג\'וקו — קניות ובידור','Shinjuku'),('שיבויה — צעירים','Shibuya'),('גינזה — יוקרה','Ginza'),('אקיהאבארה — אלקטרוניקה','Akihabara'),('אסאקוסה — מסורתי','Asakusa')],
    'Osaka':              [('דוטונבורי — אוכל ובידור','Dotonbori'),('נמבה — קניות','Namba'),('אומדה — עסקים','Umeda'),('שינסאיבאשי','Shinsaibashi')],
    'Kyoto':              [('גיון — גיישות','Gion'),('אראשיאמה — יערות במבוק','Arashiyama'),('מרכז העיר','Downtown'),('פושימי','Fushimi')],
    'Seoul':              [('גנגנאם — יוקרה','Gangnam'),('מיונגדונג — קניות','Myeongdong'),('הונגדה — צעירים ואמנות','Hongdae'),('איטאוון — בינלאומי','Itaewon'),('ג\'ונגרו — מסורתי','Jongno')],
    'Dubai':              [('דאונטאון — בורג\' ח\'ליפה','Downtown'),('דיירה — היסטורי','Deira'),('JBR — חוף','JBR'),('ביזנס ביי','Business Bay'),('פאלם — יוקרה','Palm Jumeirah')],
    'Singapore':          [('מארינה ביי — מרכז','Marina Bay'),('אורצ\'רד — קניות','Orchard'),('חיל ספייסס — בוהמה','Haji Lane'),('סנטוסה — חוף','Sentosa'),('לה ליטל אינדיה','Little India')],
    'Kuala Lumpur':       [('KLCC — מגדלים','KLCC'),('בוקיט בינטנג — קניות','Bukit Bintang'),('צ\'יינה טאון','Chinatown'),('מרדיקה — היסטורי','Merdeka')],
    'Istanbul':           [('סולטאנאהמט — היסטורי','Sultanahmet'),('בייאוגלו — מודרני','Beyoglu'),('בסיקטש — מקומיים','Besiktas'),('קאדיקוי — אסיה','Kadikoy'),('אורטאקוי — נוף','Ortakoy')],
    # אירופה
    'Paris':              [('מארה — היסטורי ובוהמה','Le Marais'),('סן ז\'רמן — ספרים וקפה','Saint-Germain'),('שאנז אליזה — יוקרה','Champs-Elysées'),('מונמארטר — אמנות','Montmartre'),('אופרה — מלונות','Opéra')],
    'Rome':               [('ספאניה — יוקרה','Spanish Steps'),('טראסטוורה — אותנטי','Trastevere'),('מרכז — קולוסיאום','Centro Storico'),('פיאצה נוונה','Navona'),('מונטי — בוהמה','Monti')],
    'Barcelona':          [('גוטיק — היסטורי','Gothic Quarter'),('איקספלה — מודרני','Eixample'),('בורנטה — צעירים','El Born'),('גראסיה — בוהמה','Gracia'),('ברצלונטה — חוף','Barceloneta')],
    'London':             [('סנטרל — ווסטמינסטר','Westminster'),('סאות\'בנק — תיאטראות','Southbank'),('שורדיץ\' — אמנות','Shoreditch'),('נוטינג היל','Notting Hill'),('קנסינגטון — מוזיאונים','Kensington')],
    'Amsterdam':          [('מרכז — תעלות','City Centre'),('ז\'ורדאן — בוטיק','Jordaan'),('דה פייפ — אותנטי','De Pijp'),('מוזיאומפליין — מוזיאונים','Museumplein')],
    'Athens':             [('פלאקה — אקרופוליס','Plaka'),('מונסטיראקי — שוק','Monastiraki'),('קולוינאקי — יוקרה','Kolonaki'),('טיסיו — בארים','Thissio')],
    'Prague':             [('מאלה סטרנה — מסורתי','Mala Strana'),('סטארה מסטו — כיכר ישנה','Stare Mesto'),('וינוהראדי — שכונת מקומיים','Vinohrady'),('ז\'יז\'קוב','Zizkov')],
    # אמריקה
    'New York':           [('מנהטן — מרכז','Manhattan'),('ברוקלין — בוהמה','Brooklyn'),('DUMBO — גלריות','DUMBO'),('אפר וסט סייד — שקט','Upper West Side'),("מידטאון — עסקים",'Midtown')],
    'Miami':              [('מיאמי ביץ\' — חוף','Miami Beach'),('ווינווד — אמנות רחוב','Wynwood'),('כוקונאט גרוב — ירוק','Coconut Grove'),('בריקל — עסקים','Brickell'),('ליטל הוואנה — קובני','Little Havana')],
    'Los Angeles':        [('סנטה מוניקה — חוף','Santa Monica'),('ויניס — בוהמה','Venice'),('בוורלי הילס — יוקרה','Beverly Hills'),('וסט הוליווד','West Hollywood'),('סילבר לייק — אמנות','Silver Lake')],
    'Cancun':             [('זון הוטלרה — חוף','Hotel Zone'),('פורטו מורלוס — שקט','Puerto Morelos'),('פלאיה דל כרמן — חיי לילה','Playa del Carmen'),('איישלה מוחרס — אי','Isla Mujeres')],
    # ישראל
    'Tel Aviv':           [('פלורנטין — בוהמה','Florentin'),('נווה צדק — בוטיק','Neve Tzedek'),('הצפון הישן — מסעדות','Old North'),('ג\'פה — היסטורי','Jaffa'),('מרכז — רוטשילד','Rothschild')],
    'Jerusalem':          [('עיר עתיקה','Old City'),('ממילא — יוקרה','Mamilla'),('גרמנית — שקט','German Colony'),('רחביה','Rehavia'),('מחנה יהודה — שוק','Mahane Yehuda')],
    'Eilat':              [('חוף צפוני','North Beach'),('חוף הדרומי — אלמוגים','South Beach'),('מרכז העיר','City Center')],
    # מצרים
    'Cairo':              [('זמאלק — שקט ויפה','Zamalek'),('גיזה — ספינקס','Giza'),('כיכר תחריר','Tahrir'),("נאסר סיטי",'Nasr City')],
    'Sharm el-Sheikh':    [('נאמה ביי — חיי לילה','Naama Bay'),('שארם אל שייח\' ישן','Old Sharm'),('חדבה — ריזורטים','Hadaba'),('קורניש','Corniche')],
    'Hurghada':           [('אל גונה — יוקרה','El Gouna'),('מרכז — דאונטאון','Downtown'),('סאקאלה — שוק','Sakala')],
    'Marrakech':          [('מדינה — שוק ומסורת','Medina'),('גוולייז — מודרני','Gueliz'),('הימלאיה — שקט','Hivernage'),('פאלמריי — ריזורטים','Palmeraie')],
    # תאילנד נוספות
    'Krabi':              [('אאו נאנג — מרכז תיירותי עם חוף','Ao Nang'),('ריילי ביץ\' — חוף ללא מכוניות (סירה בלבד)','Railay Beach'),('עיר קראבי — אותנטי ומקומי','Krabi Town'),('קלונג מואנג — ריזורטים שקטים','Klong Muang'),('קו לנטה — אי מרוחק ורגוע','Ko Lanta')],
    'Koh Samui':          [('צ\'אוונג — חיי לילה ושופינג','Chaweng'),('לאמאי — חוף רגוע','Lamai'),('בו פוט — כפר דייגים','Bo Phut'),('מאאה נאם — שקט ויוקרה','Mae Nam'),('צ\'ונג מון — שקט ומשפחתי','Chong Mon')],
    # ויאטנם נוספות
    'Da Nang':            [('חוף מיי אן — ארוך ושקט','My An Beach'),('אן תוונג — ריזורטים','An Thuong'),('חאן — מרכז העיר','Han'),('מרבל מאונטינס','Marble Mountains')],
    'Nha Trang':          [("מרכז — פרומנאד",'City Center'),('VinWonders — פארק שעשועים','VinWonders'),('באי דאי — דרום שקט','Bai Dai')],
    # מזרח תיכון נוספות
    'Muscat':             [('מוטרה — נמל ישן','Mutrah'),('קוריאת — ריזורטים','Quriyat'),('מדינת קאבוס — מודרני','Madinat Qaboos'),('השחף — חופים','Al Seeb')],
    'Doha':               [("הפנינה — יוקרה",'The Pearl'),('סוק ואקיף — מסורתי','Souq Waqif'),('לוסיל — מודרני','Lusail'),('ווסט ביי — מרכז עסקים','West Bay')],
}

# מרחקים (ק"מ בקו אוויר) ואטרקציות ייחודיות לכל אזור
NEIGHBORHOOD_INFO = {
    'Krabi': {
        'Ao Nang':      {'dist': {'Railay Beach': 3, 'Krabi Town': 22, 'Klong Muang': 15, 'Ko Lanta': 70}, 'extra': 'חוף אאו נאנג, טיולי סירות לאיים, שוק לילה, ספא'},
        'Railay Beach': {'dist': {'Ao Nang': 3, 'Krabi Town': 22, 'Klong Muang': 18}, 'extra': 'נגיש בסירה בלבד, טיפוס סלעים, חוף פרסה מושלם, East & West Railay'},
        'Krabi Town':   {'dist': {'Ao Nang': 22, 'Railay Beach': 22, 'Klong Muang': 20, 'Ko Lanta': 60}, 'extra': 'Walking Street ערב שבת, מסעדות מקומיות, ואוט טם Seua, מחירים נמוכים'},
        'Klong Muang':  {'dist': {'Ao Nang': 15, 'Krabi Town': 20, 'Railay Beach': 18}, 'extra': 'חוף שקט ופחות צפוף, ריזורטים יוקרתיים, Tubkaek Beach'},
        'Ko Lanta':     {'dist': {'Ao Nang': 70, 'Krabi Town': 60}, 'extra': 'Long Beach, Klong Dao, צלילה וסנורקלינג, אי נוח ורגוע'},
    },
    'Koh Samui': {
        'Chaweng':  {'dist': {'Lamai': 10, 'Bo Phut': 8, 'Mae Nam': 15, 'Chong Mon': 6}, 'extra': 'ביץ\' רוד חיי לילה, Ark Bar, Walking Street, קניות'},
        'Lamai':    {'dist': {'Chaweng': 10, 'Bo Phut': 16, 'Mae Nam': 22}, 'extra': 'חוף שקט מחאוונג, Grandmother & Grandfather Rock, ספא'},
        'Bo Phut':  {'dist': {'Chaweng': 8, 'Mae Nam': 6, 'Chong Mon': 10}, 'extra': 'Fisherman\'s Village, שוק שישי, מסעדות בוטיק, בוהמה'},
        'Mae Nam':  {'dist': {'Bo Phut': 6, 'Chaweng': 15, 'Lamai': 22}, 'extra': 'חוף ארוך ושקט, מחירים נמוכים, פחות תיירים'},
        'Chong Mon':{'dist': {'Chaweng': 6, 'Bo Phut': 10}, 'extra': 'מפרץ שקט, ריזורטים שקטים, מתאים למשפחות'},
    },
    'Phuket': {
        'Patong':      {'dist': {'Kata': 6, 'Karon': 5, 'Kamala': 8, 'Bang Tao': 15, 'Phuket Town': 14}, 'extra': 'Walking Street, שוק הלילה Banzaan, Bangla Road'},
        'Kata':        {'dist': {'Patong': 6, 'Karon': 3, 'Kamala': 11, 'Bang Tao': 17, 'Phuket Town': 16}, 'extra': 'חוף Kata Noi השקט, בית ספר גלישה, Bar On The Hill'},
        'Karon':       {'dist': {'Patong': 5, 'Kata': 3, 'Kamala': 10, 'Bang Tao': 14, 'Phuket Town': 13}, 'extra': 'חוף 3 ק"מ ללא המולה, סנורקלינג, Dino Park Mini Golf'},
        'Kamala':      {'dist': {'Patong': 8, 'Bang Tao': 8, 'Karon': 10, 'Phuket Town': 12}, 'extra': 'Phuket Fantasea Show, מלונות בוטיק שקטים, חוף נקי'},
        'Bang Tao':    {'dist': {'Kamala': 8, 'Patong': 15, 'Phuket Town': 12, 'Kata': 17}, 'extra': 'Laguna Resort, מגרש גולף, Boat Avenue Market'},
        'Phuket Town': {'dist': {'Patong': 14, 'Kata': 16, 'Karon': 13, 'Kamala': 12}, 'extra': 'Old Town קולוניאלי, Sunday Walking Street, מסעדות מקומיות'},
    },
    'Bangkok': {
        'Sukhumvit':   {'dist': {'Silom': 8, 'Old City': 10, 'Riverside': 9, 'Ari': 7}, 'extra': 'BTS Skytrain, Terminal 21, Terminal 49, Asok'},
        'Silom':       {'dist': {'Sukhumvit': 8, 'Riverside': 3, 'Old City': 6, 'Ari': 12}, 'extra': 'Lumpini Park, Patpong Night Market, Sky Bar Lebua'},
        'Riverside':   {'dist': {'Silom': 3, 'Old City': 4, 'Sukhumvit': 9, 'Ari': 15}, 'extra': 'נהר Chao Phraya, Asiatique, Mandarin Oriental, טיסת סירות'},
        'Old City':    {'dist': {'Riverside': 4, 'Silom': 6, 'Sukhumvit': 10, 'Ari': 14}, 'extra': 'מקדש Wat Phra Kaew, ארמון המלך, Khao San Road'},
        'Ari':         {'dist': {'Sukhumvit': 7, 'Old City': 12, 'Silom': 12, 'Riverside': 15}, 'extra': 'קפה בוטיק, שוק חי Ari, שכונה מקומית אותנטית'},
    },
    'Bali': {
        'Seminyak':    {'dist': {'Kuta': 5, 'Canggu': 8, 'Jimbaran': 10, 'Nusa Dua': 20, 'Ubud': 36}, 'extra': 'מסעדות גורמה Ku De Ta, בוטיקים, שקיעה על החוף'},
        'Kuta':        {'dist': {'Seminyak': 5, 'Jimbaran': 9, 'Canggu': 12, 'Nusa Dua': 11, 'Ubud': 38}, 'extra': 'גלישה, Beachwalk Mall, Discovery Shopping'},
        'Ubud':        {'dist': {'Seminyak': 36, 'Kuta': 38, 'Nusa Dua': 45, 'Jimbaran': 38, 'Canggu': 30}, 'extra': 'טרסות אורז Tegallalang, Sacred Monkey Forest, Agung Rai Museum'},
        'Nusa Dua':    {'dist': {'Kuta': 11, 'Jimbaran': 7, 'Seminyak': 20, 'Ubud': 45}, 'extra': 'ריזורטים 5★, חוף Geger Beach, Waterbom Bali'},
        'Jimbaran':    {'dist': {'Kuta': 9, 'Nusa Dua': 7, 'Seminyak': 10, 'Ubud': 38}, 'extra': 'מסעדות דגים על החוף, Rock Bar Ayana, שקיעה מרהיבה'},
        'Canggu':      {'dist': {'Seminyak': 8, 'Kuta': 12, 'Ubud': 30, 'Jimbaran': 18}, 'extra': 'Old Man\'s גלשנים, Black Sand Beach, Digital Nomads, Crate Café'},
    },
    'Dubai': {
        'Downtown':    {'dist': {'Business Bay': 3, 'JBR': 15, 'Deira': 20, 'Palm Jumeirah': 18}, 'extra': 'בורג\' ח\'ליפה, מזרקות הריקוד, Dubai Mall'},
        'Deira':       {'dist': {'Downtown': 20, 'Business Bay': 18, 'JBR': 28, 'Palm Jumeirah': 25}, 'extra': 'Gold Souk, Spice Souk, Dubai Creek, אוכל אותנטי'},
        'JBR':         {'dist': {'Downtown': 15, 'Palm Jumeirah': 8, 'Business Bay': 17, 'Deira': 28}, 'extra': 'The Walk, Ain Dubai, חוף JBR, Bluewaters Island'},
        'Business Bay': {'dist': {'Downtown': 3, 'JBR': 17, 'Palm Jumeirah': 20, 'Deira': 18}, 'extra': 'תעלה של דובאי, Opus Tower, מגדלי עסקים מודרניים'},
        'Palm Jumeirah': {'dist': {'JBR': 8, 'Downtown': 18, 'Business Bay': 20, 'Deira': 25}, 'extra': 'Atlantis Aquaventure, Nobu, שפודים יוקרה, Palm Monorail'},
    },
    'Tokyo': {
        'Shinjuku':    {'dist': {'Shibuya': 4, 'Ginza': 7, 'Akihabara': 5, 'Asakusa': 8}, 'extra': 'Golden Gai, Kabukicho, פארק Shinjuku Gyoen'},
        'Shibuya':     {'dist': {'Shinjuku': 4, 'Ginza': 6, 'Akihabara': 8, 'Asakusa': 10}, 'extra': 'כיכר Scramble, Harajuku, Takeshita Street'},
        'Ginza':       {'dist': {'Shibuya': 6, 'Shinjuku': 7, 'Asakusa': 7, 'Akihabara': 5}, 'extra': 'חנויות יוקרה, Kabuki-za, Tsukiji Outer Market'},
        'Akihabara':   {'dist': {'Shinjuku': 5, 'Asakusa': 4, 'Ginza': 5, 'Shibuya': 8}, 'extra': 'אלקטרוניקה, Anime, Maid Cafes, Yodobashi Camera'},
        'Asakusa':     {'dist': {'Akihabara': 4, 'Shinjuku': 8, 'Ginza': 7, 'Shibuya': 10}, 'extra': 'מקדש Senso-ji, Nakamise Dori, ריקשה, Sumida River'},
    },
    'Osaka': {
        'Dotonbori':   {'dist': {'Namba': 1, 'Umeda': 6, 'Shinsaibashi': 1}, 'extra': 'Glico Man, רחוב אוכל, טייגוסה-בו, Neon Signs'},
        'Namba':       {'dist': {'Dotonbori': 1, 'Shinsaibashi': 2, 'Umeda': 6}, 'extra': 'Kuromon Market, Den Den Town, Namba Parks'},
        'Umeda':       {'dist': {'Namba': 6, 'Dotonbori': 6, 'Shinsaibashi': 5}, 'extra': 'HEP Five Wheel, Osaka Station, Grand Front Osaka'},
        'Shinsaibashi': {'dist': {'Namba': 2, 'Dotonbori': 1, 'Umeda': 5}, 'extra': 'America-mura, קניות חינם, מועדונים'},
    },
    'Seoul': {
        'Gangnam':     {'dist': {'Myeongdong': 12, 'Hongdae': 14, 'Itaewon': 8, 'Jongno': 14}, 'extra': 'COEX Mall, K-pop YG/SM, Garosu-gil, K-Beauty'},
        'Myeongdong':  {'dist': {'Gangnam': 12, 'Hongdae': 10, 'Itaewon': 6, 'Jongno': 4}, 'extra': 'Street Food, קוסמטיקה קורנית, Lotte, N Seoul Tower'},
        'Hongdae':     {'dist': {'Gangnam': 14, 'Myeongdong': 10, 'Itaewon': 6, 'Jongno': 12}, 'extra': 'חיי לילה, אמנות רחוב, Hongik University, גלריות'},
        'Itaewon':     {'dist': {'Gangnam': 8, 'Myeongdong': 6, 'Hongdae': 6, 'Jongno': 8}, 'extra': 'מסעדות בינלאומיות, Homo Hill, Hamilton Hotel'},
        'Jongno':      {'dist': {'Myeongdong': 4, 'Itaewon': 8, 'Gangnam': 14, 'Hongdae': 12}, 'extra': 'Gyeongbokgung Palace, Insadong, Bukchon Hanok'},
    },
    'Kyoto': {
        'Gion':        {'dist': {'Arashiyama': 8, 'Downtown': 3, 'Fushimi': 5}, 'extra': 'רובע הגיישות, Hanamikoji Street, Yasaka Shrine'},
        'Arashiyama':  {'dist': {'Gion': 8, 'Downtown': 6, 'Fushimi': 10}, 'extra': 'יער הבמבוק, Tenryu-ji, גשר Togetsukyo, רכבת Sagano'},
        'Downtown':    {'dist': {'Gion': 3, 'Arashiyama': 6, 'Fushimi': 6}, 'extra': 'Nishiki Market, Pontocho Alley, Kawaramachi'},
        'Fushimi':     {'dist': {'Gion': 5, 'Downtown': 6, 'Arashiyama': 10}, 'extra': 'Fushimi Inari (שערי Torii), Gekkeikan Sake, Momoyama'},
    },
    'Paris': {
        'Le Marais':   {'dist': {'Saint-Germain': 6, 'Montmartre': 5, 'Opéra': 4, 'Champs-Elysées': 8}, 'extra': 'Place des Vosges, Centre Pompidou, LGBTQ+, גלריות'},
        'Saint-Germain': {'dist': {'Le Marais': 6, 'Champs-Elysées': 5, 'Montmartre': 9, 'Opéra': 6}, 'extra': 'Café de Flore, מוזיאון אורסה, Jardin du Luxembourg'},
        'Champs-Elysées': {'dist': {'Saint-Germain': 5, 'Montmartre': 7, 'Opéra': 4, 'Le Marais': 8}, 'extra': 'Arc de Triomphe, Louis Vuitton, מגדל אייפל 15 דק\''},
        'Montmartre':  {'dist': {'Le Marais': 5, 'Opéra': 3, 'Champs-Elysées': 7, 'Saint-Germain': 9}, 'extra': 'Sacré-Cœur, Moulin Rouge, אמנים, Place du Tertre'},
        'Opéra':       {'dist': {'Montmartre': 3, 'Le Marais': 4, 'Champs-Elysées': 4, 'Saint-Germain': 6}, 'extra': 'Galeries Lafayette, Palais Garnier, Printemps'},
    },
    'Rome': {
        'Spanish Steps': {'dist': {'Centro Storico': 5, 'Trastevere': 7, 'Monti': 5, 'Navona': 6}, 'extra': 'Trevi Fountain, Via Condotti, Villa Borghese, Piazza di Spagna'},
        'Trastevere':  {'dist': {'Centro Storico': 3, 'Spanish Steps': 7, 'Navona': 4, 'Monti': 5}, 'extra': 'ריסטורנטי מקומיים, Santa Maria, Campo de\' Fiori, חיי לילה'},
        'Centro Storico': {'dist': {'Trastevere': 3, 'Spanish Steps': 5, 'Navona': 1, 'Monti': 3}, 'extra': 'Colosseum, Pantheon, Forum Romanum, Palatine Hill'},
        'Navona':      {'dist': {'Centro Storico': 1, 'Trastevere': 4, 'Monti': 5, 'Spanish Steps': 6}, 'extra': 'Piazza Navona, Campo de\' Fiori, Sant\'Angelo Bridge'},
        'Monti':       {'dist': {'Centro Storico': 3, 'Spanish Steps': 5, 'Navona': 5, 'Trastevere': 5}, 'extra': 'שוק וינטאג\', Via Nazionale, בארים אלטרנטיביים'},
    },
    'Barcelona': {
        'Gothic Quarter': {'dist': {'El Born': 2, 'Barceloneta': 3, 'Eixample': 4, 'Gracia': 7}, 'extra': 'קתדרלת BCN, La Rambla, Plaça Reial, Picasso Museum'},
        'Eixample':    {'dist': {'Gothic Quarter': 4, 'Gracia': 3, 'El Born': 5, 'Barceloneta': 6}, 'extra': 'Sagrada Familia, Casa Batlló, Passeig de Gràcia'},
        'El Born':     {'dist': {'Gothic Quarter': 2, 'Barceloneta': 2, 'Eixample': 5, 'Gracia': 7}, 'extra': 'Picasso Museum, Bar Marsella, Santa Caterina Market'},
        'Gracia':      {'dist': {'Eixample': 3, 'Gothic Quarter': 7, 'El Born': 7, 'Barceloneta': 8}, 'extra': 'Park Güell, Plaza del Sol, שכונה מקומית בוהמיינית'},
        'Barceloneta': {'dist': {'Gothic Quarter': 3, 'El Born': 2, 'Eixample': 6, 'Gracia': 8}, 'extra': 'חוף Barceloneta, Barceloneta Aquarium, Paella על החוף'},
    },
    'London': {
        'Westminster': {'dist': {'Southbank': 2, 'Kensington': 3, 'Notting Hill': 6, 'Shoreditch': 8}, 'extra': 'Big Ben, Westminster Abbey, Buckingham Palace, Parliament'},
        'Southbank':   {'dist': {'Westminster': 2, 'Kensington': 5, 'Shoreditch': 7, 'Notting Hill': 10}, 'extra': 'Tate Modern, Globe Theatre, Borough Market, London Eye'},
        'Shoreditch':  {'dist': {'Westminster': 8, 'Southbank': 7, 'Kensington': 12, 'Notting Hill': 12}, 'extra': 'Brick Lane, Street Art Banksy, Tech Hub, Columbia Rd'},
        'Notting Hill': {'dist': {'Kensington': 3, 'Westminster': 6, 'Southbank': 10, 'Shoreditch': 12}, 'extra': 'Portobello Market, Carnival, Hyde Park, Ledbury Rd'},
        'Kensington':  {'dist': {'Westminster': 3, 'Notting Hill': 3, 'Southbank': 5, 'Shoreditch': 12}, 'extra': 'V&A Museum, Natural History Museum, Hyde Park, Harrods'},
    },
    'Amsterdam': {
        'City Centre': {'dist': {'Jordaan': 2, 'De Pijp': 3, 'Museumplein': 4}, 'extra': 'Anne Frank House, Dam Square, Royal Palace, Red Light District'},
        'Jordaan':     {'dist': {'City Centre': 2, 'De Pijp': 4, 'Museumplein': 3}, 'extra': 'West Church, Noordermarkt, מסעדות בוטיק, תעלות שקטות'},
        'De Pijp':     {'dist': {'City Centre': 3, 'Jordaan': 4, 'Museumplein': 2}, 'extra': 'Albert Cuyp Market, Heineken Experience, Gerard Douplein'},
        'Museumplein': {'dist': {'De Pijp': 2, 'City Centre': 4, 'Jordaan': 3}, 'extra': 'Rijksmuseum, Van Gogh Museum, Stedelijk, Vondelpark'},
    },
    'Istanbul': {
        'Sultanahmet': {'dist': {'Beyoglu': 6, 'Besiktas': 9, 'Kadikoy': 12, 'Ortakoy': 12}, 'extra': 'Hagia Sophia, Blue Mosque, Topkapi Palace, Grand Bazaar'},
        'Beyoglu':     {'dist': {'Sultanahmet': 6, 'Besiktas': 5, 'Ortakoy': 7, 'Kadikoy': 14}, 'extra': 'Istiklal Street, Galata Tower, Taksim Square, מסעדות'},
        'Besiktas':    {'dist': {'Beyoglu': 5, 'Sultanahmet': 9, 'Ortakoy': 3, 'Kadikoy': 12}, 'extra': 'Dolmabahce Palace, Besiktas Market, Soccer culture'},
        'Kadikoy':     {'dist': {'Sultanahmet': 12, 'Beyoglu': 14, 'Besiktas': 12, 'Ortakoy': 13}, 'extra': 'צד האסייתי, Moda, שוק Kadikoy, בארים אלטרנטיביים'},
        'Ortakoy':     {'dist': {'Besiktas': 3, 'Beyoglu': 7, 'Sultanahmet': 12, 'Kadikoy': 13}, 'extra': 'גשר הבוספורוס, Ortakoy Mosque, Kumpir, נוף מהסבוביות'},
    },
    'Dubai': {
        'Downtown':    {'dist': {'Business Bay': 3, 'JBR': 15, 'Deira': 20, 'Palm Jumeirah': 18}, 'extra': 'בורג\' ח\'ליפה, מזרקות הריקוד, Dubai Mall'},
        'Deira':       {'dist': {'Downtown': 20, 'Business Bay': 18, 'JBR': 28, 'Palm Jumeirah': 25}, 'extra': 'Gold Souk, Spice Souk, Dubai Creek, אוכל מסורתי'},
        'JBR':         {'dist': {'Downtown': 15, 'Palm Jumeirah': 8, 'Business Bay': 17, 'Deira': 28}, 'extra': 'The Walk, Ain Dubai, חוף JBR, Bluewaters Island'},
        'Business Bay': {'dist': {'Downtown': 3, 'JBR': 17, 'Palm Jumeirah': 20, 'Deira': 18}, 'extra': 'תעלת דובאי, Opus Tower, Burj Khalifa 3 דק\''},
        'Palm Jumeirah': {'dist': {'JBR': 8, 'Downtown': 18, 'Business Bay': 20, 'Deira': 25}, 'extra': 'Atlantis Aquaventure, Nobu, FIVE Hotels, Palm Monorail'},
    },
    'Tel Aviv': {
        'Florentin':   {'dist': {'Neve Tzedek': 2, 'Old North': 5, 'Jaffa': 3, 'Rothschild': 3}, 'extra': 'גרפיטי ואמנות, בארים, שוק ה-Flea Market, צעירים'},
        'Neve Tzedek': {'dist': {'Florentin': 2, 'Jaffa': 3, 'Rothschild': 2, 'Old North': 6}, 'extra': 'שכונה עתיקה, גלריות, Suzanne Dellal, ספא'},
        'Old North':   {'dist': {'Florentin': 5, 'Neve Tzedek': 6, 'Jaffa': 7, 'Rothschild': 4}, 'extra': 'חוף Gordon, Dizengoff Center, מסעדות, Bauhaus'},
        'Jaffa':       {'dist': {'Neve Tzedek': 3, 'Florentin': 3, 'Old North': 7, 'Rothschild': 4}, 'extra': 'עיר עתיקה, שוק הפשפשים, חוף, אמנים'},
        'Rothschild':  {'dist': {'Neve Tzedek': 2, 'Florentin': 3, 'Jaffa': 4, 'Old North': 4}, 'extra': 'Boulevard הבולוורד, בתי קפה, אדריכלות Bauhaus, High-Tech'},
    },
    'Singapore': {
        'Marina Bay':  {'dist': {'Orchard': 5, 'Haji Lane': 4, 'Sentosa': 9, 'Little India': 4}, 'extra': 'Marina Bay Sands, Gardens by the Bay, ArtScience Museum'},
        'Orchard':     {'dist': {'Marina Bay': 5, 'Haji Lane': 6, 'Sentosa': 12, 'Little India': 5}, 'extra': 'ION Orchard, Mandarin Gallery, Emerald Hill, קניות עולמיות'},
        'Haji Lane':   {'dist': {'Marina Bay': 4, 'Orchard': 6, 'Little India': 2, 'Sentosa': 11}, 'extra': 'בוהמה, מסעדות מוסלמיות, Sultan Mosque, Arab Quarter'},
        'Sentosa':     {'dist': {'Marina Bay': 9, 'Orchard': 12, 'Little India': 11, 'Haji Lane': 11}, 'extra': 'Universal Studios, S.E.A Aquarium, חוף Palawan'},
        'Little India': {'dist': {'Haji Lane': 2, 'Marina Bay': 4, 'Orchard': 5, 'Sentosa': 11}, 'extra': 'Tekka Market, Sri Veeramakaliamman Temple, שוק ספייסים'},
    },
    'New York': {
        'Manhattan':   {'dist': {'Brooklyn': 6, 'DUMBO': 7, 'Upper West Side': 5, 'Midtown': 3}, 'extra': 'Times Square, Central Park, MoMA, Empire State Building'},
        'Brooklyn':    {'dist': {'Manhattan': 6, 'DUMBO': 2, 'Upper West Side': 10, 'Midtown': 8}, 'extra': 'Prospect Park, Smorgasburg, Williamsburg, Brooklyn Bridge'},
        'DUMBO':       {'dist': {'Brooklyn': 2, 'Manhattan': 7, 'Midtown': 8, 'Upper West Side': 11}, 'extra': 'גלריות, נוף המנהטן, Brooklyn Flea, Jane\'s Carousel'},
        'Upper West Side': {'dist': {'Midtown': 5, 'Manhattan': 5, 'Brooklyn': 10, 'DUMBO': 11}, 'extra': 'Central Park West, Natural History Museum, Riverside Park'},
        'Midtown':     {'dist': {'Manhattan': 3, 'Upper West Side': 5, 'Brooklyn': 8, 'DUMBO': 8}, 'extra': 'Rockefeller Center, Grand Central, Bryant Park, 5th Ave'},
    },
}


@app.route('/geo/neighborhoods', methods=['GET'])
def geo_neighborhoods():
    city = request.args.get('city', '')
    data = NEIGHBORHOODS.get(city, [])
    info = NEIGHBORHOOD_INFO.get(city, {})
    result = []
    for t in data:
        en = t[1]
        nbh_info = info.get(en, {})
        result.append({
            'he':    t[0],
            'en':    en,
            'dist':  nbh_info.get('dist', {}),
            'extra': nbh_info.get('extra', ''),
        })
    print(f"[geo/neighborhoods] city={city} → {len(result)} areas")
    return Response(json.dumps(result, ensure_ascii=False), status=200, mimetype='application/json')


@app.route('/geo/countries', methods=['GET'])
def geo_countries():
    region = request.args.get('region', '').lower()
    if not region:
        return jsonify({'error': 'region required'}), 400
    data = COUNTRIES_BY_REGION.get(region, [])
    print(f"[geo/countries] region={region} → {len(data)} countries (static)")
    return Response(json.dumps(data, ensure_ascii=False), status=200, mimetype='application/json')


@app.route('/geo/cities', methods=['GET'])
def geo_cities():
    country = request.args.get('country', '')
    if not country:
        return jsonify({'error': 'country required'}), 400
    raw = CITIES_BY_COUNTRY.get(country, [])
    cities = [{'he': t[0], 'en': t[1]} for t in raw]
    print(f"[geo/cities] country={country} → {len(cities)} cities (static)")
    data = {'error': False, 'data': cities}
    return Response(json.dumps(data, ensure_ascii=False), status=200, mimetype='application/json')


@app.route('/countries', methods=['GET'])
def get_countries():
    return jsonify(list(GEO_COUNTRIES.keys()))


@app.route('/verify/hotel', methods=['POST'])
def verify_hotel():
    """Quick single search to confirm destination before full scan"""
    data       = request.json
    api_key    = _resolve_key(data.get('api_key', ''))
    query      = data.get('query', '').strip()
    check_in   = data.get('check_in')
    check_out  = data.get('check_out')
    adults     = int(data.get('adults', 2))
    rooms      = max(1, int(data.get('rooms', 1) or 1))
    max_budget     = float(data.get('max_budget', 0))
    children_ages  = data.get('children_ages', [])

    try:
        params = {
            'engine':         'google_hotels',
            'q':              query,
            'check_in_date':  check_in,
            'check_out_date': check_out,
            'adults':         adults,
            'rooms':          rooms,
            'gl':             'us',
            'hl':             'en',
            'currency':       'USD',
            'api_key':        api_key,
        }
        # ילדים 16+ נספרים כמבוגרים
        eff_adults = adults
        eff_children = []
        for age in children_ages:
            if int(age) >= 16:
                eff_adults += 1
            else:
                eff_children.append(age)
        params['adults'] = eff_adults
        if eff_children:
            params['children'] = len(eff_children)
            params['children_ages'] = ','.join(str(int(a)) for a in eff_children)

        resp = requests.get('https://serpapi.com/search', params=params, timeout=25)

        if resp.status_code == 401:
            return jsonify({'status': 'error', 'error': 'API Key שגוי — בדוק שהזנת את המפתח החדש'})
        if resp.status_code != 200:
            return jsonify({'status': 'error', 'error': f'HTTP {resp.status_code}'})

        d = resp.json()
        if 'error' in d:
            return jsonify({'status': 'error', 'error': d['error']})

        properties = d.get('properties', [])
        if not properties:
            return jsonify({'status': 'no_results', 'hotels': []})

        # Filter by budget if set
        if max_budget > 0:
            properties = [p for p in properties
                          if parse_price(p.get('rate_per_night', {}).get('lowest')) <= max_budget]

        properties = properties[:5]

        if not properties:
            return jsonify({'status': 'over_budget', 'hotels': [],
                            'message': f'לא נמצאו מלונות עד ${max_budget:.0f} ללילה'})

        hotels = []
        for p in properties:
            images = p.get('images', [])
            thumb  = images[0].get('thumbnail', '') if images else ''
            # Build location string from available fields
            location_parts = []
            if p.get('neighborhood'): location_parts.append(p['neighborhood'])
            if p.get('address'):      location_parts.append(p['address'])
            location = ', '.join(location_parts) if location_parts else p.get('description', '')
            stars = _extract_stars(p)
            hotels.append({
                'name':       p.get('name', ''),
                'stars':      stars if stars is not None else '',
                'hotel_class':p.get('hotel_class', ''),
                'rating':     p.get('overall_rating', ''),
                'price':      p.get('rate_per_night', {}).get('lowest', ''),
                'thumbnail':  thumb,
                'location':   location,
            })

        return jsonify({'status': 'ok', 'hotels': hotels})

    except requests.Timeout:
        return jsonify({'status': 'error', 'error': 'timeout — נסה שוב'})
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)})


@app.route('/search/hotels/stream', methods=['POST'])
def search_hotels_stream():
    """Stream hotel results country by country (SSE)"""
    data            = request.json or {}
    api_key         = _resolve_key(data.get('api_key', ''))
    query           = data.get('query', '').strip()
    check_in        = data.get('check_in', '')
    check_out       = data.get('check_out', '')
    adults          = max(int(data.get('adults', 2) or 2), 1)
    rooms           = max(int(data.get('rooms', 1) or 1), 1)
    children_ages   = data.get('children_ages', [])
    min_stars       = int(data.get('min_stars', 0))
    max_budget      = float(data.get('max_budget', 0))
    countries       = data.get('countries', list(GEO_COUNTRIES.keys()))

    if not api_key:
        return jsonify({'error': 'api_key required'}), 400
    if not query:
        return jsonify({'error': 'query required'}), 400
    if not check_in or not check_out:
        return jsonify({'error': 'check_in and check_out required'}), 400

    # Google Hotels API doesn't return results for stays > ~28 nights.
    # When the stay is longer, search a representative 7-night window instead.
    MAX_NIGHTS = 28
    from datetime import timedelta
    actual_nights = _calc_nights(check_in, check_out)
    search_check_in  = check_in
    search_check_out = check_out
    long_stay_note   = None
    if actual_nights > MAX_NIGHTS:
        search_check_out = (_dt.strptime(check_in, '%Y-%m-%d') + timedelta(days=7)).strftime('%Y-%m-%d')
        long_stay_note = actual_nights
        print(f"[long-stay] {actual_nights} nights → searching 7-night window {search_check_in}→{search_check_out}")

    def generate():
        if long_stay_note:
            yield f"data: {json.dumps({'status':'long_stay_note','actual_nights':long_stay_note,'search_nights':7}, ensure_ascii=False)}\n\n"

        hotel_map = {}       # in-budget hotels (keyed by name, lowest price wins)
        near_budget_map = {} # near-budget hotels (≤10% over), same dedup logic

        for country in countries:
            result = search_hotel_single(api_key, query, search_check_in, search_check_out,
                                          adults, children_ages, min_stars, max_budget, country, rooms)

            if result['status'] == 'found_many':
                country_hotels = result['hotels']
                near = result.get('near_budget', [])

                # Collect near-budget fallback regardless
                for h in near:
                    key = h['hotel_name'].strip().lower()
                    if key not in near_budget_map or h['price_num'] < near_budget_map[key]['price_num']:
                        near_budget_map[key] = h

                if country_hotels:
                    # Count only genuinely new or cheaper hotels (true unique contribution)
                    added = 0
                    for h in country_hotels:
                        key = h['hotel_name'].strip().lower()
                        if key not in hotel_map or h['price_num'] < hotel_map[key]['price_num']:
                            hotel_map[key] = h
                            added += 1

                    if added > 0:
                        pg_msg = f"{added} מלונות"
                        yield f"data: {json.dumps({'status':'progress','country':country,'message':pg_msg}, ensure_ascii=False)}\n\n"
                        merged = sorted(hotel_map.values(), key=lambda x: x['price_num'])
                        payload = {'status':'hotels_update','hotels':merged}
                        if long_stay_note:
                            payload['actual_nights'] = long_stay_note
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    else:
                        # All hotels from this country are duplicates — skip silently (no progress shown)
                        pass
                else:
                    pg_msg = "0 בתקציב" + (f" | {len(near)} קרוב לתקציב" if near else "")
                    yield f"data: {json.dumps({'status':'progress','country':country,'message':pg_msg}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"

        # If no in-budget results but near-budget hotels exist → suggest them
        if not hotel_map and near_budget_map:
            near_list = sorted(near_budget_map.values(), key=lambda x: x['price_num'])
            yield f"data: {json.dumps({'status':'suggest_near_budget','hotels':near_list}, ensure_ascii=False)}\n\n"

        # Emit best hotel for the hero card
        if hotel_map:
            best = min(hotel_map.values(), key=lambda x: x['price_num'])
            yield f"data: {json.dumps({'status':'best','best':best}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'status': 'done'}, ensure_ascii=False)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/search/flights/stream', methods=['POST'])
def search_flights_stream():
    """Stream flight results country by country (SSE)"""
    data           = request.json or {}
    api_key        = _resolve_key(data.get('api_key', ''))
    origin         = data.get('origin', '').strip().upper()
    destination    = data.get('destination', '').strip().upper()
    departure_date = data.get('departure_date', '')
    return_date    = data.get('return_date', '')
    adults         = max(int(data.get('adults', 1) or 1), 1)
    # Default: India + Singapore (proven cheapest geo-pricing for flights)
    DEFAULT_FLIGHT_COUNTRIES = ['🇮🇳 הודו', '🇸🇬 סינגפור']
    countries      = data.get('countries', DEFAULT_FLIGHT_COUNTRIES)

    if not api_key:
        return jsonify({'error': 'api_key required'}), 400
    if not origin or len(origin) < 2:
        return jsonify({'error': 'origin IATA code required (min 2 chars)'}), 400
    if not destination or len(destination) < 2:
        return jsonify({'error': 'destination IATA code required (min 2 chars)'}), 400
    if not departure_date:
        return jsonify({'error': 'departure_date required'}), 400

    def generate():
        found = []
        for country in countries:
            result = search_flight_single(api_key, origin, destination,
                                           departure_date, return_date, adults, country)
            if result['status'] == 'found':
                found.append(result)
            yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"

        if found:
            best = min(found, key=lambda r: r['price_num'])
            yield f"data: {json.dumps({'status': 'best', 'best': best}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'status': 'done'}, ensure_ascii=False)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── ATTRACTION HELPERS ──────────────────────────────────────────────

_AGE_RULES = [
    (['night club','nightclub',' bar','bar,','bar.','casino','gambling','adult entertainment'], '18+', '🔞'),
    (['children museum',"children's museum",'kids museum','playground','family entertainment'], 'לילדים', '👶'),
    (['theme park','amusement park','water park','waterpark','funfair',
      'disneyland','disney','universal studios','legoland','eurodisnee','eurodisney',
      'roller coaster','rollercoaster','fun park','adventure park'], 'כל הגילאים', '🎢'),
    (['zoo','aquarium','safari','wildlife'], 'כל הגילאים', '🦁'),
    (['hiking','trekking','zip line','bungee','rock climbing','extreme'], '8+', '🥾'),
    (['museum','gallery','palace','castle','cathedral','temple','shrine','mosque','church'], 'כל הגילאים', '🏛️'),
    (['beach','park','garden','viewpoint','lookout','market'], 'כל הגילאים', '🌿'),
    (['spa','massage','wellness'], '16+', '💆'),
]

_DURATION_RULES = [
    (['theme park','amusement park','water park','waterpark','zoo','aquarium','safari'], 'יום שלם', '⏰'),
    (['museum','gallery','palace','castle'], '1–3 שעות', '⏱️'),
    (['temple','shrine','mosque','church','cathedral'], '30–60 דקות', '⏱️'),
    (['viewpoint','lookout','observation'], '15–30 דקות', '⏱️'),
    (['market','bazaar','souk','shopping'], '1–3 שעות', '⏱️'),
    (['tour','cruise','boat','day trip'], '3–6 שעות', '⏱️'),
    (['beach','park','garden'], '1–4 שעות', '⏱️'),
]

# מחיר ממוצע (אמצע הטווח) לחישוב סה"כ
_PRICE_MAP = {'$': (5, 20), '$$': (15, 50), '$$$': (40, 120), '$$$$': (100, 300)}
_PRICE_MID = {'$': 12, '$$': 30, '$$$': 75, '$$$$': 180}

# זיהוי סוג הפעילות לפי מילות מפתח בשם ובתיאור
_ACTIVITY_KEYWORDS = [
    # ספורט אתגרי
    (['rock climbing','rock climb','cliff climbing'],        'טיפוס סלעים 🧗',        '8+'),
    (['bungee','bungy'],                                      "קפיצת בנג'י 🤸",         '16+'),
    (['skydiving','sky diving','skydive'],                    'צניחה חופשית ✈️',        '18+'),
    (['paragliding','para gliding','paraglide'],              'פאראגליידינג 🪂',         '12+'),
    (['zip line','zipline','flying fox'],                     'זיפ-ליין 🌉',            '8+'),
    (['white water','rafting','river rafting'],               'ספורט מים סוערים 🌊',    '12+'),
    (['surfing','surf lesson','surf class'],                  'גלישת גלים 🏄',          '10+'),
    (['wakeboard','wake board'],                              'ווייקבורד 🏄',           '12+'),
    (['atv','quad bike','off road','offroad'],                'רכב שטח ATV 🏍️',        '8+'),
    (['motorbike tour','motorbike','scooter tour'],           'סיור אופנוע 🏍️',         '18+'),
    (['horse riding','horseback'],                            'רכיבה על סוסים 🐴',      'כל הגילאים'),
    (['bicycle tour','cycling tour','bike tour'],             'סיור אופניים 🚲',        'כל הגילאים'),
    (['kayak','kayaking','sea kayak'],                        'קיאקינג 🚣',             'כל הגילאים'),
    (['stand up paddle','sup '],                              'SUP גלשן עמידה 🏄',      'כל הגילאים'),
    (['trekking','hiking tour','jungle trek'],                'טרק בטבע 🥾',            '8+'),
    # ים ומים
    (['scuba','diving course','dive shop','dive center'],     'צלילת סקובה 🤿',         '10+'),
    (['snorkeling','snorkel'],                                'סנורקלינג 🐠',           'כל הגילאים'),
    (['boat tour','boat trip','longtail','speedboat'],        'סיור בסירה ⛵',          'כל הגילאים'),
    (['island hop','island tour','island trip'],              'סיור איים 🏝️',           'כל הגילאים'),
    (['sunset cruise','dinner cruise','river cruise'],        'שייט 🛥️',               'כל הגילאים'),
    (['fishing','fish tour'],                                 'דיג 🎣',                 'כל הגילאים'),
    # תרבות
    (['cooking class','cook class','thai cook','cooking school'], 'שיעור בישול 👨‍🍳',   'כל הגילאים'),
    (['muay thai','martial art','boxing'],                    'אומנויות לחימה 🥊',      '8+'),
    (['meditation','yoga retreat','yoga class'],              'מדיטציה ויוגה 🧘',      'כל הגילאים'),
    (['painting class','art class','pottery'],                'שיעור אמנות 🎨',         'כל הגילאים'),
    (['night tour','ghost tour','city tour'],                 'סיור מודרך 🗺️',          'כל הגילאים'),
    (['food tour','street food tour'],                        'סיור אוכל 🍜',           'כל הגילאים'),
    (['night market','walking street','sunday market'],       'שוק לילה 🌙',            'כל הגילאים'),
    # בעלי חיים
    (['elephant sanctuary','elephant camp','ethical elephant'],'מקלט פילים 🐘',        'כל הגילאים'),
    (['elephant ride','elephant show'],                       'רכיבה על פיל 🐘',       'כל הגילאים'),
    (['tiger','tiger temple','tiger kingdom'],                'ביקור נמרים 🐯',         '12+'),
    (['monkey','monkey show'],                                "ביקור קופים 🐒",         'כל הגילאים'),
    (['bird park','butterfly park','reptile'],                'פארק בעלי חיים 🦜',      'כל הגילאים'),
    # ספא ורפואה
    (['thai massage','traditional massage','oil massage'],    'עיסוי תאי 💆',           '16+'),
    (['spa','hot spring','thermal'],                          'ספא ומרחץ 🛁',           '16+'),
    # שונות
    (['escape room','escape game'],                           'חדר בריחה 🔐',           '8+'),
    (['go kart','karting','racing'],                          'מירוץ קארטינג 🏎️',       '8+'),
    (['shooting range','gun range'],                          'מטווח 🎯',               '18+'),
    (['wine tasting','brewery tour','beer tour'],             'טעימות 🍷',              '18+'),
    (['hot air balloon','balloon ride'],                      'כדור פורח 🎈',           '8+'),
    (['helicopter tour','helicopter ride'],                   'סיור במסוק 🚁',          'כל הגילאים'),
]

def _infer_activity_type(title: str, desc_str: str) -> tuple:
    """Return (activity_label_he, min_age_override) from title+description keywords."""
    combined = (title + ' ' + desc_str).lower()
    for keywords, label, min_age in _ACTIVITY_KEYWORDS:
        if any(k in combined for k in keywords):
            return label, min_age
    return None, None  # no specific activity detected

def _infer_age(types_str: str, desc_str: str) -> tuple:
    combined = (types_str + ' ' + desc_str).lower()
    for keywords, label, icon in _AGE_RULES:
        if any(k in combined for k in keywords):
            return label, icon
    return 'כל הגילאים', '✅'

def _infer_duration(types_str: str) -> tuple:
    t = types_str.lower()
    for keywords, label, icon in _DURATION_RULES:
        if any(k in t for k in keywords):
            return label, icon
    return '1–2 שעות', '⏱️'

def _price_range(symbol: str) -> str:
    if not symbol:
        return 'חינם / לא ידוע'
    lo, hi = _PRICE_MAP.get(symbol, (0, 0))
    return f'~${lo}–${hi}' if lo else 'חינם'

def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    import math
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def _enrich_attractions(raw_list: list) -> list:
    """Enrich raw SerpAPI local results with inferred fields and nearby places."""
    enriched = []
    for p in raw_list:
        title     = p.get('title', '')
        gps       = p.get('gps_coordinates', {})
        type_raw  = p.get('type', '')
        types_str = type_raw if isinstance(type_raw, str) else ' '.join(type_raw or [])
        desc_str  = p.get('description', '')
        price_sym = p.get('price', '')

        # Specific activity detection (title-based) overrides generic type
        activity_label, activity_age = _infer_activity_type(title, desc_str)
        age_label, age_icon = _infer_age(types_str, desc_str)
        if activity_age:
            age_label = activity_age
            age_icon  = '⚠️' if '+' in activity_age and activity_age != 'כל הגילאים' else '✅'
        dur_label, dur_icon = _infer_duration(types_str + ' ' + title.lower())

        # Price: use SerpAPI symbol if available, else try extreme-specific estimate
        price_mid = _PRICE_MID.get(price_sym, 0)
        if not price_mid:
            price_mid = _extreme_price(title, desc_str)

        enriched.append({
            'name':           title,
            'address':        p.get('address', ''),
            'rating':         p.get('rating', ''),
            'reviews':        p.get('reviews', 0),
            'type':           types_str.strip() or 'אטרקציה',
            'activity_label': activity_label or '',   # specific activity in Hebrew
            'description':    desc_str[:200],
            'hours':          p.get('hours', ''),
            'price_symbol':   price_sym,
            'price_range':    _price_range(price_sym),
            'price_mid':      price_mid,              # midpoint $ per person
            'thumbnail':      p.get('thumbnail', ''),
            'website':        p.get('website', ''),
            'maps_link':      p.get('links', {}).get('directions', '') or
                              (f"https://www.google.com/maps/search/?api=1&query={gps.get('latitude','')},{gps.get('longitude','')}" if gps else ''),
            'gps':            gps,
            'age_label':      age_label,
            'age_icon':       age_icon,
            'duration':       dur_label,
            'dur_icon':       dur_icon,
        })
    # Add nearby (3 closest by GPS)
    for i, a in enumerate(enriched):
        gps_a = a.get('gps', {})
        if not gps_a:
            a['nearby'] = []
            continue
        dists = []
        for j, b in enumerate(enriched):
            if i == j: continue
            gps_b = b.get('gps', {})
            if not gps_b: continue
            try:
                d = _haversine_km(gps_a['latitude'], gps_a['longitude'],
                                   gps_b['latitude'], gps_b['longitude'])
                dists.append((d, b['name']))
            except Exception:
                pass
        dists.sort()
        a['nearby'] = [name for _, name in dists[:3]]
    return enriched


# מחירים ממוצעים ריאליסטיים לפעילויות אקסטרים ($ לאדם)
_EXTREME_PRICE = {
    'bungee': 65, 'bungy': 65,
    'skydiving': 220, 'sky diving': 220, 'skydive': 220,
    'paragliding': 110, 'paraglide': 110,
    'zip line': 35, 'zipline': 35, 'flying fox': 35,
    'scuba': 80, 'dive': 75, 'diving': 75,
    'shark': 140,
    'snorkeling': 25, 'snorkel': 25,
    'white water': 50, 'rafting': 50,
    'surfing': 45, 'surf': 45,
    'rock climbing': 60, 'cliff climbing': 60,
    'atv': 55, 'quad': 55,
    'hot air balloon': 160, 'balloon ride': 160,
    'helicopter': 190,
    'kayak': 30,
    'wakeboard': 55,
    'kitesur': 90,
    'motorbike tour': 40,
    'horse riding': 35, 'horseback': 35,
    'escape room': 20,
    'go kart': 25, 'karting': 25,
    'disneyland': 110, 'disney': 110,
    'universal studios': 100, 'universal': 100,
    'legoland': 75,
    'water park': 40, 'waterpark': 40,
    'theme park': 65, 'amusement park': 55,
    'roller coaster': 50, 'rollercoaster': 50,
    'adventure park': 45,
}

def _extreme_price(title: str, desc: str) -> int:
    """Return estimated price for extreme activity, or 0 if not extreme."""
    combined = (title + ' ' + desc).lower()
    for kw, price in _EXTREME_PRICE.items():
        if kw in combined:
            return price
    return 0


# מיפוי מדינה → קוד GL לחיפוש ממוקד
COUNTRY_GL = {
    'Thailand':'th','Japan':'jp','Indonesia':'id','Vietnam':'vn','Singapore':'sg',
    'Malaysia':'my','Philippines':'ph','South Korea':'kr','China':'cn','Taiwan':'tw',
    'Cambodia':'kh','Nepal':'np','Sri Lanka':'lk','Turkey':'tr','India':'in',
    'United Arab Emirates':'ae','Qatar':'qa','Jordan':'jo','Israel':'il',
    'France':'fr','Italy':'it','Spain':'es','Germany':'de','United Kingdom':'gb',
    'Greece':'gr','Portugal':'pt','Netherlands':'nl','Austria':'at','Switzerland':'ch',
    'Czech Republic':'cz','Hungary':'hu','Poland':'pl','Croatia':'hr',
    'United States':'us','Canada':'ca','Mexico':'mx','Brazil':'br','Argentina':'ar',
    'Australia':'au','New Zealand':'nz','Egypt':'eg','Morocco':'ma','South Africa':'za',
    'Kenya':'ke','Tanzania':'tz',
}


def search_attractions_city(api_key: str, city: str, country: str,
                             trip_purpose: str, children_ages: list,
                             categories: list = None) -> dict:
    """Fetch attractions — searches from destination GL + India for cheapest operators."""
    dest_gl = COUNTRY_GL.get(country, 'us')
    # For google_maps (local results), geo-pricing doesn't apply — only destination GL matters
    # India GL adds no value for local attraction searches and wastes API credits
    search_gls = [dest_gl]

    def _call(q, gl='us'):
        params = {
            'engine':  'google_maps',
            'q':       q,
            'type':    'search',
            'hl':      'en',
            'gl':      gl,
            'api_key': api_key,
        }
        r = requests.get('https://serpapi.com/search', params=params, timeout=25)
        print(f"[attractions] q={q!r} status={r.status_code}")
        if r.status_code == 401: return None, 'API Key שגוי'
        if r.status_code != 200:
            try:    return None, r.json().get('error', f'HTTP {r.status_code}')
            except: return None, f'HTTP {r.status_code}'
        d = r.json()
        print(f"  ↳ keys={list(d.keys())[:6]}")
        if 'error' in d: return None, d['error']
        # google_maps returns local_results
        results = d.get('local_results', [])
        print(f"  ↳ local_results={len(results)}")
        return results, None

    categories = categories or ['general']
    is_extreme = 'extreme' in categories
    is_parks   = 'parks'   in categories

    # Build query — always include city+country for global accuracy
    dest = f"{city}, {country}" if country else city

    if is_parks:
        query = f'theme parks amusement parks water parks family entertainment {dest}'
    elif is_extreme:
        query = f'extreme sports adventure activities {dest}'
    else:
        purpose_map = {
            'family':  f'family friendly attractions {dest}',
            'couple':  f'romantic places {dest}',
            'solo':    f'things to do {dest}',
            'general': f'top tourist attractions {dest}',
        }
        query = purpose_map.get(trip_purpose, f'top tourist attractions {dest}')
        if children_ages and trip_purpose != 'family':
            query = f'family activities {dest}'

    # Build secondary + tertiary complementary queries for more variety
    secondary_query = f'things to do {dest}'
    tertiary_query  = None
    if is_parks:
        secondary_query = f'water park splash pool entertainment {dest}'
        tertiary_query  = f'family activities kids {dest}'
    elif is_extreme:
        secondary_query = f'adventure outdoor sports {dest}'
        tertiary_query  = f'outdoor activities excursions {dest}'
    else:
        tertiary_query  = f'popular places visit {dest}'

    # All queries to run
    queries = [q for q in [query, secondary_query, tertiary_query] if q]

    # Search from each GL with each query, merge by title (dedup), keep best rating
    merged: dict = {}  # title.lower() → raw result
    broadened = False
    MIN_RESULTS = 4

    def _merge(results_list):
        for p in results_list:
            key = p.get('title','').strip().lower()
            if not key: continue
            existing = merged.get(key)
            if not existing:
                merged[key] = p
            else:
                r_new = p.get('rating', 0) or 0
                r_old = existing.get('rating', 0) or 0
                if r_new > r_old:
                    merged[key] = p

    for q in queries:
        if len(merged) >= 20:
            break  # enough results — skip remaining queries to save API credits
        for gl in search_gls:
            results, err = _call(q, gl)
            if err:
                print(f"  [gl={gl}] error: {err}")
                continue
            if not results:
                continue
            print(f"  [gl={gl}] q={q[:40]!r} → {len(results)} results")
            _merge(results)

    # If still fewer than MIN_RESULTS — broaden with generic query
    if len(merged) < MIN_RESULTS:
        broad = f'popular places to visit {dest}'
        for gl in search_gls[:1]:  # just destination GL
            results, _ = _call(broad, gl)
            if results:
                _merge(results)
                broadened = city + ' (חיפוש מורחב)'
                print(f"  broadened → {len(merged)} total")

    if not merged:
        return {'status': 'no_results', 'city': city, 'error': 'לא נמצאו אטרקציות ביעד זה'}

    enriched = _enrich_attractions(list(merged.values())[:25])
    print(f"[attractions] {city} → {len(enriched)} unique results (broadened={broadened})")
    return {'status': 'found', 'city': city, 'attractions': enriched, 'broadened': broadened}


@app.route('/search/attractions/stream', methods=['POST'])
def search_attractions_stream():
    """Stream attraction results city by city (SSE)"""
    data          = request.json
    api_key       = _resolve_key(data.get('api_key', ''))
    cities        = data.get('cities', [])
    trip_purpose  = data.get('trip_purpose', 'general')
    children_ages = data.get('children_ages', [])
    categories    = data.get('categories', ['general'])

    if not cities:
        return jsonify({'error': 'cities required'}), 400

    def generate():
        for item in cities:
            city    = item.get('city', '')
            country = item.get('country', '')
            yield f"data: {json.dumps({'status':'city_start','city':city}, ensure_ascii=False)}\n\n"

            result = search_attractions_city(api_key, city, country, trip_purpose, children_ages, categories)

            if result['status'] == 'found':
                for attr in result['attractions']:
                    yield f"data: {json.dumps({'status':'attraction','city':city,'attraction':attr,'broadened':result['broadened']}, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'status':'city_done','city':city,'count':len(result['attractions']),'broadened':result['broadened']}, ensure_ascii=False)}\n\n"
            else:
                yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'status':'done'}, ensure_ascii=False)}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    host = '0.0.0.0' if os.environ.get('PORT') else '127.0.0.1'
    try:
        print(f"  Travel Price Agent — Ready on {host}:{port}")
    except UnicodeEncodeError:
        pass
    app.run(debug=False, port=port, host=host)
