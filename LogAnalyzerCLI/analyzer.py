import os

def fileAnalyzer(parsedLogs):
    keywords = ["error", "failed", "stopped" , "warning"]

    with open(parsedLogs, 'r') as f:
        lines = f.readlines()

    print("Parsed Lines:")
    print("-----------------------")
    for line in lines:
        print(line.strip())

    print("\nSummary:")
    for keyword in keywords:
        count = sum(1 for line in lines if keyword in line.lower())
        print(f"Total Number for '{keyword}': {count}")

    os.remove(parsedLogs)