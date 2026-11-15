import requests

# Replace with your ESP32 IP
ESP32_IP = "172.20.10.2"

while True:

    cmd = input("Enter command (open/close/exit): ").lower()

    try:

        if cmd == "open":

            url = f"http://{ESP32_IP}/open"

            response = requests.get(url)

            print("ESP32 Response:", response.text)

        elif cmd == "close":

            url = f"http://{ESP32_IP}/close"

            response = requests.get(url)

            print("ESP32 Response:", response.text)

        elif cmd == "exit":

            print("Exiting...")
            break

        else:

            print("Invalid command")

    except Exception as e:

        print("Connection Error:", e)