import os

def fileValidate(path):
    os.system("cls")
    if os.path.exists(path):
        print(f"This file {path} exists and I am working on parsing logs")
        print("-------------------------------------------------------------------")
        return True
    else:
        print(f"This file {path} does not exist, Please check again!")
        print("-------------------------------------------------------------------")
        return False
