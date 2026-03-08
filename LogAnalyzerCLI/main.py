import validater
import parser
import analyzer

logFile = input("Path of your log file: ")

if not validater.fileValidate(logFile):
    print(f"{logFile} does not exist, please check again!")
    exit()

parsedLogs = parser.fileParser(logFile)

if not parsedLogs:
    print("We ran into an issue, please try again later.")
    exit()

analyzer.fileAnalyzer(parsedLogs)