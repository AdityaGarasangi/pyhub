import random
from datetime import datetime, timedelta

# Normal operational messages
normal_messages = [
    "User logged in successfully",
    "Session created",
    "Session terminated",
    "Cache cleared successfully",
    "Database query executed",
    "Configuration loaded",
    "Background job executed",
    "Heartbeat check OK",
    "File uploaded successfully",
    "Email notification sent",
    "API responded with 200 OK",
    "Data synced with remote server",
    "Service restarted successfully",
    "User profile updated",
    "Scheduled task completed"
]

# Error / failure messages
error_messages = [
    "ERROR: Could not connect to database",
    "ERROR: Timeout connecting to API",
    "ERROR: Permission denied",
    "Failed to process user request",
    "FAILED: Transaction aborted",
    "FAILED: Unable to allocate resources",
    "STOPPED: Service interrupted",
    "Error parsing input data",
    "Error writing to log file",
    "STOPPED: Network connection lost",
    "Failed to send email notification",
    "Error: Disk space critically low",
    "ERROR: Invalid authentication token",
    "FAILED: Dependency service unavailable",
    "Error fetching user profile"
]

# Generate 50–100 lines with timestamps
log_file_path = "./tests/logs.txt"
start_time = datetime.now() - timedelta(days=1)

with open(log_file_path, "w") as f:
    for i in range(69000):
        # Random timestamp in the past 24 hours
        ts = start_time + timedelta(seconds=random.randint(0, 86400))
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        
        # Randomly pick normal or error message
        if random.random() < 0.65:
            msg = random.choice(normal_messages)
            level = "INFO"
        else:
            msg = random.choice(error_messages)
            level = "ERROR"
        
        # Optional extra details to mimic prod logs
        request_id = f"req-{random.randint(1000,9999)}"
        user_id = f"user-{random.randint(1,50)}"
        endpoint = f"/api/v{random.randint(1,3)}/resource"
        
        log_line = f"{ts_str} [{level}] ({request_id}) [user:{user_id}] {endpoint} - {msg}\n"
        f.write(log_line)