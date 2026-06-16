from events.events import Event

class EventBus:

    def __init__(self):
        self.subscribers = {}   # event_type → list of functions

    def subscribe(self, event_type, handler):
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)

    def publish(self, event: Event):
        print(f"EVENT TRIGGERED: {event.type}")

        handlers = self.subscribers.get(event.type, [])

        for handler in handlers:
            handler(event.payload)