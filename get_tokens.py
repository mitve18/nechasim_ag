import os

from google_auth_oauthlib.flow import InstalledAppFlow

# ההרשאות שהמערכת צריכה לדרייב
SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    print("פותח חלון דפדפן לאימות מול גוגל...")
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    creds = flow.run_local_server(port=0)
    
    # הרשאות לבעלים בלבד - הקובץ מכיל Refresh Token ארוך-טווח
    fd = os.open('token.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as token_file:
        token_file.write(creds.to_json())
    os.chmod('token.json', 0o600)  # למקרה שהקובץ כבר היה קיים עם הרשאות רחבות יותר

    print("הקובץ token.json נוצר בהצלחה!")

if __name__ == '__main__':
    main()