# Python Image ကို အခြေခံအဖြစ် အသုံးပြုခြင်း
FROM python:3.11-slim

# FFmpeg နှင့် လိုအပ်သော System Library များကို စက်ထဲသို့ သွင်းခြင်း
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# လုပ်ငန်းလုပ်ဆောင်မည့် စက်ထဲက Folder နာမည် သတ်မှတ်ခြင်း
WORKDIR /app

# လိုအပ်သော Library စာရင်း (requirements.txt) ကို အရင်ကူးခြင်း
COPY requirements.txt .

# Library များကို Install လုပ်ခြင်း
RUN pip install --no-cache-dir -r requirements.txt

# ကျန်ရှိသော Code ဖိုင်အားလုံးကို ကူးထည့်ခြင်း
COPY . .

# Flask Development Server အစား Video Processing အတွက် ခံနိုင်ရည်ရှိသော Gunicorn ကို ပြောင်းသုံးခြင်း
# (Timeout ကို စက္ကန့် ၃၀၀ အထိ ပေးထားသဖြင့် Video Edit လုပ်နေစဉ် Connection ပြတ်မကျသွားစေပါ)
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app", "--timeout", "300"]
