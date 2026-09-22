import socket
import ssl

hostname = 'trainee.api.planetcloud.cloud'
context = ssl.create_default_context()

with socket.create_connection((hostname, 443)) as sock:
    with context.wrap_socket(sock, server_hostname=hostname) as ssock:
        request = f"GET /api/sse/test HTTP/1.1\r\nHost: {hostname}\r\nAccept: text/event-stream\r\nConnection: close\r\n\r\n"
        ssock.sendall(request.encode())
        
        while True:
            data = ssock.recv(1024)
            if not data:
                break
            print(f"Received {len(data)} bytes:")
            print(data.decode(errors='replace'))
