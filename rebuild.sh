#!/bin/bash
# Rebuild Plexbie Discord Bot Docker image

echo "Stopping existing container..."
docker stop plexbie 2>/dev/null
docker rm plexbie 2>/dev/null

echo ""
echo "=============================================="
echo "Building new Docker image..."
echo "=============================================="
cd /mnt/user/appdata/plexbie/plexbie
docker build -t plexbie:latest .

echo "Starting container with new image..."
docker run -d \
  --name plexbie \
  --restart unless-stopped \
  -v /mnt/user/appdata/plexbie/config:/app/config \
  -v /mnt/user/appdata/plexbie/plexbie/logs:/app/logs \
  -v /mnt/user/appdata/plexbie/plexbie/plugins:/app/plugins \
  -p 8081:8081 \
  plexbie:latest

echo "Checking container status..."
sleep 3
docker ps | grep plexbie

echo "Showing recent logs..."
docker logs plexbie --tail 50
