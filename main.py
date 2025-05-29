import argparse
import enum
import random
import time
from concurrent import futures

import grpc
from google.protobuf import json_format
from grpc import RpcError

from internal.handler.coms import game_pb2
from internal.handler.coms import game_pb2_grpc as game_grpc

MOVES = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


class Movements(enum.Enum):
    BOT_LEFT = (-1, -1)
    LEFT = (-1, 0)
    TOP_LEFT = (-1, 1)
    TOP = (0, 1)
    TOP_RIGHT = (1, 1)
    RIGHT = (1, 0)
    BOT_RIGHT = (1, -1)
    BOT = (0, -1)

timeout_to_response = 1  # 1 second


class BotGameTurn:
    def __init__(self, turn, action):
        self.turn = turn
        self.action = action


class BotGame:
    def __init__(self, player_num=None):
        self.player_num = player_num
        self.initial_state = None
        self.turn_states = []
        self.countT = 1

    def new_turn_action(self, turn: game_pb2.NewTurn) -> game_pb2.NewAction:
        cx, cy = turn.Position.X, turn.Position.Y

        lighthouses = dict()
        for lh in turn.Lighthouses:
            lighthouses[(lh.Position.X, lh.Position.Y)] = lh

        # Si estamos en un faro...
        if (cx, cy) in lighthouses:
            # Conectar con faro remoto válido si podemos
            if lighthouses[(cx, cy)].Owner == self.player_num:
                possible_connections = []
                for dest in lighthouses:
                    # No conectar con sigo mismo
                    # No conectar si no tenemos la clave
                    # No conectar si ya existe la conexión
                    # No conectar si no controlamos el destino
                    # Nota: no comprobamos si la conexión se cruza.
                    if (
                        dest != (cx, cy)
                        and lighthouses[dest].HaveKey
                        and [cx, cy] not in lighthouses[dest].Connections
                        and lighthouses[dest].Owner == self.player_num
                    ):
                        possible_connections.append(dest)

                if possible_connections:
                    possible_connection = random.choice(possible_connections)
                    action = game_pb2.NewAction(
                        Action=game_pb2.CONNECT,
                        Destination=game_pb2.Position(
                            X=possible_connection[0], Y=possible_connection[1]
                        ),
                    )
                    bgt = BotGameTurn(turn, action)
                    self.turn_states.append(bgt)

                    self.countT += 1
                    return action

            # 60% de posibilidades de atacar el faro
            if random.randrange(100) < 60:
                energy = random.randrange(turn.Energy + 1)
                action = game_pb2.NewAction(
                    Action=game_pb2.ATTACK,
                    Energy=energy,
                    Destination=game_pb2.Position(X=turn.Position.X, Y=turn.Position.Y),
                )
                bgt = BotGameTurn(turn, action)
                self.turn_states.append(bgt)

                self.countT += 1
                return action

        # Mover aleatoriamente

        # Buscar el faro apropiado basado en el ratio
        # Movernos en la direcciñon adecuada, dandole nuestra posicion y la del faro que buscamos

        number_of_conquered_lighthouses = sum(1 for lighthouse in turn.Lighthouses if lighthouse.Owner == self.player_num)
        if number_of_conquered_lighthouses > 20:
            my_lighthouses = [lighthouse for lighthouse in turn.Lighthouses if lighthouse.Owner == self.player_num]
            chosen_lighthouse = my_lighthouses[0]

        else:
            lighthouses_ratio = {}
            for lighthouse in turn.Lighthouses:
                ratio = self.compute_ratio(turn.Position, lighthouse)
                lighthouses_ratio[(lighthouse.Position.X, lighthouse.Position.Y, lighthouse.Owner)] = ratio

            chosen_lighthouse = self.get_chosen_non_conquered_lighthouse(lighthouses_ratio)
        next_movement = self.get_next_movement(turn.Position, chosen_lighthouse)
        move = next_movement
        action = game_pb2.NewAction(
            Action=game_pb2.MOVE,
            Destination=game_pb2.Position(
                X=turn.Position.X + move[0], Y=turn.Position.Y + move[1]
            ),
        )

        bgt = BotGameTurn(turn, action)
        self.turn_states.append(bgt)

        self.countT += 1
        return action

    def compute_ratio(self, our_position, lighthouse):
        energy = lighthouse.Energy
        distance = abs(our_position.X - lighthouse.Position.X) + abs(our_position.Y - lighthouse.Position.Y)
        ratio = 1 / ((energy+1) * (distance + 1))
        return ratio

    def get_chosen_non_conquered_lighthouse(self, all_lighthouses):
        filtered = {lh: ratio for lh, ratio in all_lighthouses.items() if lh[2] != self.player_num}
        return max(filtered, key=filtered.get) if filtered else None

    def get_next_movement(self, current_position, target_lighthouse):
        dy = target_lighthouse[1] - current_position.Y
        dx = target_lighthouse[0] - current_position.X
        if dy > 0:
            return Movements.TOP.value
        elif dy < 0:
            return Movements.BOT.value
        elif dx > 0:
            return Movements.RIGHT.value
        return Movements.LEFT.value

class BotComs:
    def __init__(self, bot_name, my_address, game_server_address, verbose=False):
        self.bot_id = None
        self.bot_name = bot_name
        self.my_address = my_address
        self.game_server_address = game_server_address
        self.verbose = verbose

    def wait_to_join_game(self):
        channel = grpc.insecure_channel(self.game_server_address)
        client = game_grpc.GameServiceStub(channel)

        player = game_pb2.NewPlayer(name=self.bot_name, serverAddress=self.my_address)

        while True:
            try:
                player_id = client.Join(player, timeout=timeout_to_response)
                self.bot_id = player_id.PlayerID
                print(f"Joined game with ID {player_id.PlayerID}")
                if self.verbose:
                    print(json_format.MessageToJson(player_id))
                break
            except RpcError as e:
                print(f"Could not join game: {e.details()}")
                time.sleep(1)

    def start_listening(self):
        print("Starting to listen on", self.my_address)

        # configure gRPC server
        grpc_server = grpc.server(
            futures.ThreadPoolExecutor(max_workers=10),
            interceptors=(ServerInterceptor(),),
        )

        # registry of the service
        cs = ClientServer(bot_id=self.bot_id, verbose=self.verbose)
        game_grpc.add_GameServiceServicer_to_server(cs, grpc_server)

        # server start
        grpc_server.add_insecure_port(self.my_address)
        grpc_server.start()

        try:
            grpc_server.wait_for_termination()  # wait until server finish
        except KeyboardInterrupt:
            grpc_server.stop(0)


class ServerInterceptor(grpc.ServerInterceptor):
    def intercept_service(self, continuation, handler_call_details):
        start_time = time.time_ns()
        method_name = handler_call_details.method

        # Invoke the actual RPC
        response = continuation(handler_call_details)

        # Log after the call
        duration = time.time_ns() - start_time
        print(f"Unary call: {method_name}, Duration: {duration:.2f} nanoseconds")
        return response


class ClientServer(game_grpc.GameServiceServicer):
    def __init__(self, bot_id, verbose=False):
        self.bg = BotGame(bot_id)
        self.verbose = verbose

    def Join(self, request, context):
        return None

    def InitialState(self, request, context):
        print("Receiving InitialState")
        if self.verbose:
            print(json_format.MessageToJson(request))
        self.bg.initial_state = request
        return game_pb2.PlayerReady(Ready=True)

    def Turn(self, request, context):
        print(f"Processing turn: {self.bg.countT}")
        if self.verbose:
            print(json_format.MessageToJson(request))
        action = self.bg.new_turn_action(request)
        return action


def ensure_params():
    parser = argparse.ArgumentParser(description="Bot configuration")
    parser.add_argument("--bn", type=str, default="random-bot", help="Bot name")
    parser.add_argument("--la", type=str, required=True, help="Listen address")
    parser.add_argument("--gs", type=str, required=True, help="Game server address")

    args = parser.parse_args()

    if not args.bn:
        raise ValueError("Bot name is required")
    if not args.la:
        raise ValueError("Listen address is required")
    if not args.gs:
        raise ValueError("Game server address is required")

    return args.bn, args.la, args.gs


def main():
    verbose = False
    bot_name, listen_address, game_server_address = ensure_params()

    bot = BotComs(
        bot_name=bot_name,
        my_address=listen_address,
        game_server_address=game_server_address,
        verbose=verbose,
    )
    bot.wait_to_join_game()
    bot.start_listening()


if __name__ == "__main__":
    main()
