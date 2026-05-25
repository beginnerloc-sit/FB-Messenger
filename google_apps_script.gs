// Paste this into Google Apps Script (script.google.com → New project),
// then Deploy → New deployment → type "Web app" → execute as Me,
// access "Anyone". Copy the /exec URL into SHEET_WEBHOOK_URL.
//
// Sheet must exist and be named "Messages" (or change SHEET_NAME below).
// Optional: set SHARED_SECRET to require the caller to send {"secret": "..."}.

const SHEET_NAME = "Messages";
const SHARED_SECRET = "";  // leave empty to disable check

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);
    if (SHARED_SECRET && body.secret !== SHARED_SECRET) {
      return ContentService.createTextOutput(JSON.stringify({error: "forbidden"}))
        .setMimeType(ContentService.MimeType.JSON);
    }

    const rows = body.rows || [];
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(SHEET_NAME);
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_NAME);
      sheet.appendRow(["received_at", "fb_timestamp", "sender_id", "name", "phone", "message", "message_id"]);
    }

    const now = new Date();
    rows.forEach(r => {
      sheet.appendRow([
        now,
        r.timestamp || "",
        r.sender_id || "",
        r.name || "",
        r.phone || "",
        r.message || "",
        r.message_id || "",
      ]);
    });

    return ContentService.createTextOutput(JSON.stringify({ok: true, written: rows.length}))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({error: String(err)}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}
