def fileParser(logFile):

    keywords = ["error", "failed", "stopped" , "warning"]

    with open(logFile, 'r') as logs, open("parser_logs_temp", 'w') as parsed_log:
        for line in logs:
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in keywords):
                parsed_log.write(line)

    return "parser_logs_temp"