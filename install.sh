#!/bin/bash

# installing deps
sudo apt update
sudo apt install -y python3-venv nodejs npm

# see if .venv exists
if [ ! -d ".venv" ]; then
    echo ".venv does not exist, creating..."
    python3 -m venv .venv
fi

# see if virutal env is activated
if [ -z "$VIRTUAL_ENV" ]; then
    echo ".venv is not activated. Activating..."
    source .venv/bin/activate
fi

echo "Installing requirements..."
pip3 install -r requirements.txt || exit 1

echo "Installing pm2..."
sudo npm install -g pm2

echo "Starting bot..."
pm2 start main.py --interpreter=.venv/bin/python --name "tg-persona"

echo ">>>remember to run 'pm2 flush' to remove tg password from logs!!<<<"