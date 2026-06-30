import urllib.request, re
url = 'https://www.ordertracker.com/track/1Z3R22440497009789'
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
req = urllib.request.Request(url, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10) as f:
        html = f.read().decode('utf-8')
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if match:
            print('Found NEXT_DATA')
            print(match.group(1)[:500])
        else:
            print('No NEXT_DATA')
            match2 = re.search(r'\"events\":\s*\[', html)
            if match2:
                print('Found events array')
            else:
                print('No events array found.')
                print(html[-1000:])
except Exception as e:
    print('Error:', e)
