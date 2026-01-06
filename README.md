# install

cd ~
sudo apt update -y
sudo apt install -y curl

curl -fsSL "https://gist.githubusercontent.com/778777266/63ab0499cee0297c81cf0fffd751d805/raw/deploy.sh" -o deploy.sh
chmod +x deploy.sh
sudo bash deploy.sh

# print token

sudo cat /root/deploy-sync-upload-token.txt

# upload

curl -sS -H "X-Upload-Token: <your token>" \
 -F "file=@./filename.bin" \
 "https://yourdomain/upload?key=abc"

# download

curl -sS "https://yourdomain/download/<task_id>"
