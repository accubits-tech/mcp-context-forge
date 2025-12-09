#!/bin/bash
# Test script for LLM Chat flow

set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Testing LLM Chat Flow${NC}"
echo "================================"

# Configuration
BASE_URL="http://localhost:4444"
USER_ID="test_user_$(date +%s)"
SERVER_ID="8359a3823ce742c5a79e5698dc4d54fa"

# Get JWT token (you need to set this)
JWT_TOKEN="${MCPGATEWAY_BEARER_TOKEN:-}"

if [ -z "$JWT_TOKEN" ]; then
    echo -e "${RED}Error: JWT_TOKEN not set${NC}"
    echo "Please set MCPGATEWAY_BEARER_TOKEN environment variable"
    echo "Example: export MCPGATEWAY_BEARER_TOKEN=\$(python3 -m mcpgateway.utils.create_jwt_token --username admin@example.com --exp 0 --secret \$(grep JWT_SECRET_KEY .env | cut -d'=' -f2))"
    exit 1
fi

echo -e "${YELLOW}Using User ID: ${USER_ID}${NC}"
echo ""

# Step 1: Connect
echo -e "${YELLOW}Step 1: Calling /llmchat/connect${NC}"
CONNECT_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/llmchat/connect" \
  -H "Authorization: Bearer ${JWT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"${USER_ID}\",
    \"server\": {
      \"url\": \"${BASE_URL}/servers/${SERVER_ID}/mcp\",
      \"transport\": \"streamable_http\"
    },
    \"llm\": {
      \"provider\": \"openai\",
      \"config\": {
        \"api_key\": \"sk-test\",
        \"model\": \"gpt-4o-mini\",
        \"temperature\": 0.7
      }
    },
    \"streaming\": true
  }")

HTTP_CODE=$(echo "$CONNECT_RESPONSE" | tail -n 1)
RESPONSE_BODY=$(echo "$CONNECT_RESPONSE" | sed '$d')

echo "HTTP Status: ${HTTP_CODE}"
echo "Response: ${RESPONSE_BODY}"

if [ "$HTTP_CODE" -ne 200 ]; then
    echo -e "${RED}Connect failed with HTTP ${HTTP_CODE}${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Connect successful${NC}"
echo ""

# Step 2: Check status
echo -e "${YELLOW}Step 2: Checking session status${NC}"
STATUS_RESPONSE=$(curl -s "${BASE_URL}/llmchat/status/${USER_ID}")
echo "Status Response: ${STATUS_RESPONSE}"

IS_CONNECTED=$(echo "$STATUS_RESPONSE" | grep -o '"connected":[^,}]*' | cut -d':' -f2)
if [ "$IS_CONNECTED" = "true" ]; then
    echo -e "${GREEN}✓ Session exists${NC}"
else
    echo -e "${RED}✗ Session does not exist${NC}"
    exit 1
fi
echo ""

# Step 3: Send chat message
echo -e "${YELLOW}Step 3: Sending chat message${NC}"
CHAT_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${BASE_URL}/llmchat/chat" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"${USER_ID}\",
    \"message\": \"Hello, this is a test message\",
    \"streaming\": false
  }")

HTTP_CODE=$(echo "$CHAT_RESPONSE" | tail -n 1)
RESPONSE_BODY=$(echo "$CHAT_RESPONSE" | sed '$d')

echo "HTTP Status: ${HTTP_CODE}"
echo "Response: ${RESPONSE_BODY}"

if [ "$HTTP_CODE" -eq 200 ]; then
    echo -e "${GREEN}✓ Chat successful${NC}"
else
    echo -e "${RED}✗ Chat failed with HTTP ${HTTP_CODE}${NC}"
    exit 1
fi
echo ""

# Step 4: Disconnect
echo -e "${YELLOW}Step 4: Disconnecting${NC}"
DISCONNECT_RESPONSE=$(curl -s -X POST "${BASE_URL}/llmchat/disconnect" \
  -H "Content-Type: application/json" \
  -d "{
    \"user_id\": \"${USER_ID}\"
  }")

echo "Disconnect Response: ${DISCONNECT_RESPONSE}"
echo -e "${GREEN}✓ Disconnect successful${NC}"
echo ""

echo -e "${GREEN}All tests passed!${NC}"
