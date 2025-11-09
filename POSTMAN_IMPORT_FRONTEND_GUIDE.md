# Postman Collection Import - Frontend Integration Guide

## Overview

This guide provides complete details for frontend developers on how the Postman Collection import feature is integrated into the MCP Gateway Admin UI.

## Feature Summary

The Postman Collection import feature allows users to:
- Upload a Postman Collection v2.1 JSON file
- Paste Postman Collection JSON directly into a textarea
- Automatically convert all requests to MCP tools
- Preview collection details before import (name, request count, auth type)
- Import tools with automatic format detection

---

## Backend API

### Endpoint: `POST /admin/tools/import`

**Auto-Detection**: The backend automatically detects whether the payload is a Postman Collection or a JSON array of tools.

**Request Format**:
```http
POST /admin/tools/import HTTP/1.1
Content-Type: application/json
Authorization: Bearer <token>

{
  "info": {
    "name": "Hospital API's",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "item": [
    {
      "name": "GET specialties",
      "request": {
        "method": "GET",
        "url": {
          "raw": "https://hospital-appointment.m.nxtgen.cloud/api/specialties/",
          "host": ["https://hospital-appointment.m.nxtgen.cloud"],
          "path": ["api", "specialties", ""]
        }
      }
    }
  ]
}
```

**Response Format**:
```json
{
  "success": true,
  "total": 7,
  "imported": 7,
  "failed": 0,
  "message": "Successfully imported all 7 tools",
  "created_count": 7,
  "failed_count": 0,
  "created": [
    {"index": 0, "name": "get_specialties"},
    {"index": 1, "name": "get_hospitals"}
  ],
  "errors": [],
  "details": {
    "success": ["get_specialties", "get_hospitals", ...],
    "failed": []
  }
}
```

**Error Response**:
```json
{
  "success": false,
  "message": "Failed to parse Postman collection: Invalid JSON structure",
  "total": 0,
  "imported": 0,
  "failed": 0
}
```

---

## Frontend UI Components

### 1. HTML Structure (admin.html)

#### Bulk Import Modal
The modal has been updated to support both JSON arrays and Postman Collections:

```html
<!-- Format Detection Info Box -->
<div id="import-format-info" class="hidden rounded-lg border border-blue-200 bg-blue-50 p-3">
  <div class="flex items-start gap-2">
    <svg class="h-5 w-5 text-blue-600" fill="currentColor" viewBox="0 0 20 20">
      <path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0z..." clip-rule="evenodd"/>
    </svg>
    <div class="flex-1">
      <p class="text-sm font-medium text-blue-800" id="format-detected-text">
        Format Detected
      </p>
      <div id="format-details" class="mt-1 text-sm text-blue-700"></div>
    </div>
  </div>
</div>

<!-- Updated Textarea Placeholder -->
<textarea
  id="tools_json"
  placeholder="[{&quot;name&quot;:&quot;tool_name&quot;,...}]

or Postman Collection v2.1:
{&quot;info&quot;:{&quot;name&quot;:&quot;My API&quot;,...},&quot;item&quot;:[...]}"
></textarea>

<!-- Updated File Input Label -->
<label for="tools_file">
  Or upload JSON file (Tools Array or Postman Collection)
</label>
```

---

### 2. JavaScript Functions

#### Format Detection

```javascript
/**
 * Detect import format (Postman Collection or JSON array)
 * @param {Object|Array} data - Parsed JSON data
 * @returns {string} - 'postman', 'tool-array', or 'unknown'
 */
window.detectImportFormat = function (data) {
  if (!data || typeof data !== 'object') {
    return 'unknown';
  }

  // Check for Postman Collection
  const info = data.info || {};
  const schema = info.schema || '';
  if (schema.toLowerCase().includes('postman') || (data.item && info.name)) {
    return 'postman';
  }

  // Check for JSON array of tools
  if (Array.isArray(data)) {
    return 'tool-array';
  }

  return 'unknown';
};
```

#### Postman Collection Preview

```javascript
/**
 * Display Postman collection information
 * @param {Object} collection - Postman collection object
 */
window.showPostmanCollectionInfo = function (collection) {
  const infoDiv = document.getElementById('import-format-info');
  const detailsDiv = document.getElementById('format-details');

  if (!infoDiv || !detailsDiv) return;

  const info = collection.info || {};
  const collectionName = info.name || 'Unnamed Collection';
  const requestCount = countPostmanRequests(collection.item || []);
  const authType = collection.auth ? collection.auth.type : 'None';

  detailsDiv.innerHTML = `
    <div class="space-y-1">
      <p><strong>Collection:</strong> ${escapeHtml(collectionName)}</p>
      <p><strong>Requests:</strong> ${requestCount} (will be converted to ${requestCount} tools)</p>
      <p><strong>Auth:</strong> ${escapeHtml(authType)}</p>
      <p class="text-xs mt-2">The collection will be automatically converted to MCP tools upon import.</p>
    </div>
  `;

  document.getElementById('format-detected-text').textContent = '✓ Postman Collection Detected';
  infoDiv.classList.remove('hidden');
};
```

#### Request Counter (Recursive)

```javascript
/**
 * Count requests in Postman collection (handles nested folders)
 * @param {Array} items - Postman items array
 * @returns {number} - Total request count
 */
window.countPostmanRequests = function (items) {
  let count = 0;
  if (!Array.isArray(items)) return 0;

  for (const item of items) {
    if (item.request) {
      count++; // This is a request
    }
    if (item.item && Array.isArray(item.item)) {
      count += countPostmanRequests(item.item); // Recurse into folder
    }
  }
  return count;
};
```

#### HTML Escaping Utility

```javascript
/**
 * Escape HTML to prevent XSS
 * @param {string} text - Text to escape
 * @returns {string} - Escaped HTML
 */
window.escapeHtml = function (text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
};
```

#### Updated File Handler

```javascript
window.handleFileSelect = function (input) {
  const file = input.files[0];
  if (!file) {
    document.getElementById("file-info").classList.add("hidden");
    return;
  }

  // Validate file type
  if (!file.name.toLowerCase().endsWith(".json")) {
    showImportError("Please select a JSON file.");
    input.value = "";
    return;
  }

  // Read file content
  const reader = new FileReader();
  reader.onload = function (e) {
    try {
      const jsonData = JSON.parse(e.target.result);

      // Detect format
      const format = detectImportFormat(jsonData);

      // Show Postman collection info if detected
      if (format === 'postman') {
        showPostmanCollectionInfo(jsonData);
      }

      processImportData(jsonData);
    } catch (error) {
      showImportError(`Invalid JSON file: ${error.message}`);
      input.value = "";
    }
  };
  reader.readAsText(file);
};
```

#### Updated Textarea Validator

```javascript
window.validateJsonInput = function () {
  const jsonText = document.getElementById("bulk-import-json").value.trim();
  const statusElement = document.getElementById("json-validation-status");

  if (!jsonText) {
    statusElement.textContent = "Please enter JSON data";
    return;
  }

  try {
    const jsonData = JSON.parse(jsonText);

    // Detect format
    const format = detectImportFormat(jsonData);

    // Handle Postman collection
    if (format === 'postman') {
      showPostmanCollectionInfo(jsonData);
      statusElement.textContent = "✓ Valid Postman Collection detected";
      statusElement.className = "text-sm text-green-600";
      document.getElementById("import-submit-btn").disabled = false;
      bulkImportData = jsonData;
      return;
    }

    // Continue with array validation...
    if (!Array.isArray(jsonData)) {
      statusElement.textContent = "✗ JSON must be an array of tool objects";
      // ... error handling
    }

  } catch (error) {
    statusElement.textContent = `✗ Invalid JSON: ${error.message}`;
  }
};
```

---

## Postman Collection Structure Reference

### Supported Format: v2.1

```json
{
  "info": {
    "name": "My API Collection",
    "description": "Optional description",
    "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  "auth": {
    "type": "bearer",
    "bearer": [{
      "key": "token",
      "value": "{{bearerToken}}"
    }]
  },
  "variable": [
    {
      "key": "baseUrl",
      "value": "https://api.example.com"
    }
  ],
  "item": [
    {
      "name": "Get Users",
      "request": {
        "method": "GET",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "url": {
          "raw": "{{baseUrl}}/api/users?limit=10",
          "host": ["https://api.example.com"],
          "path": ["api", "users"],
          "query": [
            {
              "key": "limit",
              "value": "10"
            }
          ]
        }
      }
    },
    {
      "name": "Create User",
      "request": {
        "method": "POST",
        "header": [
          {
            "key": "Content-Type",
            "value": "application/json"
          }
        ],
        "body": {
          "mode": "raw",
          "raw": "{\"name\":\"John\",\"email\":\"john@example.com\"}",
          "options": {
            "raw": {
              "language": "json"
            }
          }
        },
        "url": {
          "raw": "{{baseUrl}}/api/users",
          "host": ["https://api.example.com"],
          "path": ["api", "users"]
        }
      }
    }
  ]
}
```

---

## Conversion Rules: Postman → MCP Tools

### 1. **Basic Mapping**

| Postman Field | MCP Tool Field | Notes |
|---------------|----------------|-------|
| `item[].name` | `displayName` | Human-readable name |
| `item[].name` (sanitized) | `name` | Lowercase, underscores, folder path prefix |
| `request.method` | `request_type` | GET, POST, PUT, DELETE, PATCH |
| `request.url` | `url` | Full URL (base + path) |
| `item[].description` | `description` | Optional description |

### 2. **URL Handling**

```javascript
// Postman URL object
{
  "raw": "https://api.example.com/users?limit=10",
  "host": ["https://api.example.com"],  // Can be full URL or parts
  "path": ["users"],
  "query": [{"key": "limit", "value": "10"}]
}

// Converted to MCP
{
  "url": "https://api.example.com/users",
  "base_url": "https://api.example.com",
  "path_template": "/users",
  "input_schema": {
    "type": "object",
    "properties": {
      "limit": {"type": "string", "default": "10"}
    }
  }
}
```

### 3. **Authentication Conversion**

| Postman Auth Type | MCP Auth Type | Conversion |
|-------------------|---------------|------------|
| `bearer` | `bearer` | Extract token, create Authorization header |
| `basic` | `basic` | Base64 encode username:password |
| `apikey` | `authheaders` | Custom header with API key |
| `oauth2` | `oauth` | Map OAuth config |

### 4. **Request Body**

- **Raw JSON**: Converted to `input_schema.properties.body`
- **Form Data**: Converted to `input_schema.properties.formdata`
- **URL Encoded**: Converted to `input_schema.properties.urlencoded`

### 5. **Folders**

Nested folders are flattened into tool names and tags:

```
Folder: "API v2" → "Users"
Request: "Get User"

Converted tool:
{
  "name": "api_v2_users_get_user",
  "displayName": "Get User",
  "tags": ["postman_import", "api_v2", "users", "get"]
}
```

---

## User Workflow

### Step 1: Open Bulk Import Modal
Click "Import Tools" button in Admin UI → Tools section

### Step 2: Choose Input Method

**Option A: Upload File**
1. Click "Choose File"
2. Select Postman Collection `.json` file
3. File is read and format auto-detected
4. Postman preview appears (if collection detected)

**Option B: Paste JSON**
1. Copy Postman Collection JSON
2. Paste into textarea
3. Format auto-detected on input
4. Postman preview appears

### Step 3: Review Preview

If Postman Collection detected:
- Blue info box appears
- Shows: Collection name, Request count, Auth type
- Indicates tools will be auto-generated

If JSON array detected:
- Standard tool array validation
- Shows tool count

### Step 4: Submit Import

1. Click "Import Tools" button
2. Loading indicator appears
3. Backend converts Postman → Tools
4. Success/error message displays
5. Tools table refreshes automatically

---

## Testing Checklist

### ✅ Format Detection
- [ ] Detects Postman Collection v2.1
- [ ] Detects Postman Collection v2.0
- [ ] Detects JSON array of tools
- [ ] Returns 'unknown' for invalid JSON

### ✅ Preview Display
- [ ] Shows collection name
- [ ] Counts requests correctly (including nested folders)
- [ ] Displays auth type
- [ ] Hides preview for non-Postman JSON

### ✅ File Upload
- [ ] Accepts `.json` files
- [ ] Rejects non-JSON files
- [ ] Shows Postman preview for collections
- [ ] Shows tool array preview for arrays

### ✅ Textarea Input
- [ ] Real-time validation
- [ ] Shows Postman preview on paste
- [ ] Shows tool array preview for arrays
- [ ] Displays helpful error messages

### ✅ Import Success
- [ ] Backend accepts Postman collection
- [ ] Converts all requests to tools
- [ ] Returns correct success count
- [ ] Refreshes tools table
- [ ] Shows success message

### ✅ Error Handling
- [ ] Invalid JSON → clear error message
- [ ] Empty collection → "No tools generated" error
- [ ] Malformed Postman format → parsing error
- [ ] Network errors → timeout message

---

## Example: Hospital API Collection

**Input** (Postman Collection):
```json
{
  "info": {
    "name": "Hospital API's"
  },
  "item": [
    {
      "name": "GET specialties",
      "request": {
        "method": "GET",
        "url": {
          "raw": "https://hospital-appointment.m.nxtgen.cloud/api/specialties/"
        }
      }
    },
    {
      "name": "GET Hospitals",
      "request": {
        "method": "GET",
        "url": {
          "raw": "https://hospital-appointment.m.nxtgen.cloud/api/hospitals/?specialty=Neurology&location=Kengeri",
          "query": [
            {"key": "specialty", "value": "Neurology"},
            {"key": "location", "value": "Kengeri"}
          ]
        }
      }
    },
    {
      "name": "Make appointments",
      "request": {
        "method": "POST",
        "body": {
          "mode": "raw",
          "raw": "{\"doctor_id\":\"123\",\"patient_name\":\"John\"}",
          "options": {"raw": {"language": "json"}}
        },
        "url": {
          "raw": "https://hospital-appointment.m.nxtgen.cloud/api/appointments"
        }
      }
    }
  ]
}
```

**Output** (7 MCP Tools Generated):
1. `get_specialties` (GET)
2. `get_hospitals` (GET, params: specialty, location)
3. `get_doctor_by_specialty` (GET, param: hospital_name)
4. `make_appointments` (POST, body schema)
5. `get_appointments_doctor` (GET)
6. `update_appointment` (PATCH)
7. `get_appointments_users` (GET)

**Tags**: `["postman_import", "hospital", "demo", "get|post|patch"]`

---

## Debugging Tips

### 1. Enable Browser Console

```javascript
// Check what format was detected
console.log('Detected format:', detectImportFormat(jsonData));

// Verify request count
console.log('Request count:', countPostmanRequests(collection.item));

// Check preview display
console.log('Preview visible:', !document.getElementById('import-format-info').classList.contains('hidden'));
```

### 2. Backend Logs

Check for these log messages:
```
INFO - Detected Postman Collection format
INFO - Generated 7 tools from collection 'Hospital API's'
ERROR - Failed to convert Postman collection: <error>
```

### 3. Network Tab

Verify POST request to `/admin/tools/import`:
- Status: 200 OK
- Response: `{"success": true, "imported": 7}`

---

## CSS Classes Used

- `hidden` - Hide/show preview box
- `text-green-600` - Success message color
- `text-red-600` - Error message color
- `text-blue-800` - Info box text color
- `bg-blue-50` - Info box background
- `border-blue-200` - Info box border

---

## Browser Compatibility

- ✅ Chrome 90+
- ✅ Firefox 88+
- ✅ Safari 14+
- ✅ Edge 90+

Requires:
- FileReader API
- JSON.parse()
- ES6 arrow functions
- Template literals

---

## Security Considerations

1. **XSS Prevention**: Always use `escapeHtml()` when displaying user input
2. **File Size Limits**: Max 10MB for uploaded files (enforced by browser)
3. **JSON Validation**: All JSON is validated before processing
4. **CSRF Protection**: Bearer token required in Authorization header
5. **Rate Limiting**: 10 requests/minute (configurable)

---

## Future Enhancements

- [ ] Support Postman Collection v3.0
- [ ] Support OpenAPI 3.x import
- [ ] Support Swagger 2.0 import
- [ ] Drag-and-drop file upload
- [ ] Bulk collection import (multiple files)
- [ ] Collection preview with folder tree visualization
- [ ] Export tools to Postman Collection format

---

## Support

For issues or questions:
- GitHub: https://github.com/chrishayuk/mcp-contextforge/issues
- Docs: https://docs.contextforge.dev
- Email: support@contextforge.dev
