import Foundation
import Starscream

final class DeviceConnection: WebSocketDelegate {
    private let host: String
    private let token: String
    private let dispatcher: CommandDispatcher
    private let reconnect = Reconnect()

    private var socket: WebSocket?
    private var keepAliveTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var stopped = false
    private var commandTasks: [UUID: Task<Void, Never>] = [:]

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
        cancelCommands()
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
        reconnectTask = Task { @MainActor [weak self] in
            guard let self else { return }
            let delay = await self.reconnect.nextDelay()
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            if Task.isCancelled || self.stopped { return }
            self.openSocket()
        }
    }

    // MARK: WebSocketDelegate

    func didReceive(event: WebSocketEvent, client: WebSocketClient) {
        // Starscream delivers callbacks on the main queue. Ignore late callbacks
        // from sockets superseded by a reconnect or explicit disconnect.
        guard let source = client as? WebSocket, source === socket else { return }
        switch event {
        case .connected:
            AppState.shared.setConnection(.connected)
            Task { @MainActor [weak self] in await self?.reconnect.reset() }
            startKeepAlive()
        case .disconnected(let reason, let code):
            NSLog("DeviceConnection disconnected: code=\(code) reason=\(reason)")
            stopKeepAlive()
            cancelCommands()
            socket = nil
            if !stopped { AppState.shared.setConnection(.disconnected) }
            scheduleReconnect()
        case .text(let s):
            if let data = s.data(using: .utf8) { enqueue(data: data, source: source) }
        case .binary(let data):
            enqueue(data: data, source: source)
        case .ping, .pong:
            break
        case .viabilityChanged, .reconnectSuggested:
            break
        case .cancelled:
            stopKeepAlive()
            cancelCommands()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.disconnected)
                scheduleReconnect()
            }
        case .error(let err):
            let msg = err?.localizedDescription ?? "unknown"
            NSLog("DeviceConnection error: \(msg)")
            stopKeepAlive()
            cancelCommands()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.error(msg))
                scheduleReconnect()
            }
        case .peerClosed:
            stopKeepAlive()
            cancelCommands()
            socket = nil
            if !stopped {
                AppState.shared.setConnection(.disconnected)
                scheduleReconnect()
            }
        }
    }

    private func startKeepAlive() {
        keepAliveTask?.cancel()
        keepAliveTask = Task { @MainActor [weak self] in
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

    private func cancelCommands() {
        for task in commandTasks.values { task.cancel() }
        commandTasks.removeAll()
    }

    private func enqueue(data: Data, source: WebSocket) {
        let taskID = UUID()
        commandTasks[taskID] = Task { @MainActor [weak self, weak source] in
            guard let self, let source else { return }
            defer { self.commandTasks.removeValue(forKey: taskID) }
            guard !Task.isCancelled, !self.stopped, self.socket === source else { return }
            let response = await self.dispatcher.dispatch(data)
            guard !Task.isCancelled, !self.stopped, self.socket === source,
                  let text = String(data: response, encoding: .utf8) else { return }
            source.write(string: text)
        }
    }
}
