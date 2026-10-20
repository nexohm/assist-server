"""Password dell'accesso amministratore: solo in memoria, per pochi minuti.

Non finiscono mai nel database né nei backup: se il server viene copiato o violato in un secondo
momento, non c'è nessuna password da trovare. Dopo TTL secondi (o al riavvio) spariscono; per
vederne una nuova l'installatore preme di nuovo «Mostra password» e l'agente ne genera un'altra.
"""
import threading
import time

TTL = 15 * 60
_lock = threading.Lock()
_items = {}  # plant_id -> (scadenza, comando, utente, password)


def put(command_id, plant_id, username, password):
    with _lock:
        _items[plant_id] = (time.time() + TTL, command_id, username, password)


def get(plant_id):
    with _lock:
        item = _items.get(plant_id)
        if item and item[0] < time.time():
            _items.pop(plant_id, None)
            item = None
    return {"expires": item[0], "username": item[2], "password": item[3]} if item else None


def drop(plant_id):
    with _lock:
        _items.pop(plant_id, None)
