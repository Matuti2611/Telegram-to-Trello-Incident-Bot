import asyncio
from prisma import Prisma

async def cargar_datos():
    db = Prisma()
    await db.connect()

    # Acá ponés todas las direcciones que ya tenés.
    # IMPORTANTE: Escribí las direcciones en minúsculas para que coincida
    # con la normalización que hace el bot.
    datos_iniciales = [
        {"direccion": "Ricchieri 960", "clienteId": "NN"},
        {"direccion": "Ricchieri 962", "clienteId": "NN"},
        {"direccion": "Ituzaingo 333", "clienteId": "IZ"},
        {"direccion": "Montevideo 1523", "clienteId": "MG"},
        # ... seguí agregando las tuyas acá copiando el mismo formato ...
    ]

    print(f"Iniciando la carga de {len(datos_iniciales)} direcciones...")

    for item in datos_iniciales:
        try:
            # Usamos 'upsert' (Update o Insert). 
            # Esto es clave: si la dirección no existe, la crea. 
            # Si ya existe, actualiza el ID. Así podés correr este script 
            # varias veces sin que tire error por duplicados.
            await db.clienteubicacion.upsert(
                where={"direccion": item["direccion"]},
                data={
                    "create": {
                        "direccion": item["direccion"],
                        "clienteId": item["clienteId"]
                    },
                    "update": {
                        "clienteId": item["clienteId"]
                    }
                }
            )
            print(f"✅ Cargado: {item['direccion']} -> {item['clienteId']}")
        except Exception as e:
            print(f"❌ Error cargando {item['direccion']}: {e}")

    print("¡Carga inicial completada con éxito!")
    await db.disconnect()

if __name__ == '__main__':
    asyncio.run(cargar_datos())