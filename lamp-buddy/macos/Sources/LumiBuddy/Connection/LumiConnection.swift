import Foundation
import Starscream

final class LumiConnection: WebSocketDelegate {
    private let host: String
    private let token: String
    private let dispatcher: CommandDispatcher
    private let reconnect = Reconnect()

    private var socket: WebSocket?
    private var keepAliveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var stopped = false

    init(host: String, token: String, dispatcher: CommandDispatcher) {
        self.host = host
        self.token = token
        self.dispatcher = dispatcher
    }

    func connect() {
        guard socket == nil else { return }
        stopped = false
        openSocket()
    }

    func disconnect() {
        stopped = true
        keepAliveTask?.cancel()
        keepAliveTask = nil
        reconnectTask?.cancel()
        reconnectTask = nil
        socket?.disconnect()
        socket = nil
        AppState.shared.setConnection(.disconnected)
    }

    private func openSocket() {
        guard let url = URL(string: "ws://\(host)/api/buddy/ws") else {
            AppState.shared.setConnection(.error("invalid host: \(host)"))
            return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 30
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let ws = WebSocket(request: req)
        ws.delegate = self
        socket = ws
        AppState.shared.setConnection(.connecting)
        ws.connect()
    }

    private func scheduleReconnect() {
        guard !stopped else { return }
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            guard let self else { return }
            let delay = await self.reconnect.nextDelay()
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if Task.isCancelled || self.stopped { return }
            self.openSocket()
        }
    }

    // MARK: WebSocketDelegate

    func didReceive(event: WebSocketEvent, client: WebSocketClient) {
        switch event {
        case .connected:
            AppState.shared.setConnection(.connected)
            Task { [weak self] in await self?.reconnect.reset() }
            startKeepAlive()
        case .disconnected(let reason, let code):
            NSLog("LumiConnection disconnected: code=\(code) reason=\(reason)")
            stopKeepAlive()
            socket = nil
            if !stopped { AppState.shared.setConnection(.disconnected) }
            scheduleReconnect()
        case .text(let s):
            if let data = s.data(using: .utf8) { Task { await self.handle(data: data) } }
        case .binary(let data):
            Task { await self.handle(data: data) }
        case .ping, .pong:
            break
        case .viabilityChanged, .reconnectSuggested:
            break
        case .cancelled:
            stopKeepAlive()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.disconnected)
                scheduleReconnect()
            }
        case .error(let err):
            let msg = err?.localizedDescription ?? "unknown"
            NSLog("LumiConnection error: \(msg)")
            stopKeepAlive()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.error(msg))
                scheduleReconnect()
            }
        case .peerClosed:
            stopKeepAlive()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.disconnected)
                scheduleReconnect()
            }
        }
    }

    private func startKeepAlive() {
        keepAliveTask?.cancel()
        keepAliveTask = Task { [weak self] in
            while let self, !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if Task.isCancelled { break }
                self.socket?.write(ping: Data())
            }
        }
    }

    private func stopKeepAlive() {
        keepAliveTask?.cancel()
        keepAliveTask = nil
    }

    private func handle(data: Data) async {
        let response = await dispatcher.dispatch(data)
        guard let str = String(data: response, encoding: .utf8) else { return }
        socket?.write(string: str)
    }
}
