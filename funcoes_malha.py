"""
Funções auxiliares para a análise de conectividade da malha aérea doméstica
brasileira (VRA - ANAC). Pensado para ser importado em um Jupyter notebook:

    from funcoes_malha import *

Espera os CSVs mensais do VRA numa pasta (ex.: VRA_20251.csv ... VRA_202512.csv).
A linha 1 do arquivo é "Atualizado em: ..." e o cabeçalho está na linha 2.

Colunas usadas:
  ICAO Aeródromo Origem/Destino -> vértices e arestas do grafo
  Situação Voo                  -> só REALIZADO vira aresta; CANCELADO entra na taxa de cancelamento
  Código Tipo Linha             -> filtro (N = nacional)
  ICAO Empresa Aérea            -> nº de empresas e dependência de uma companhia por aeroporto
  Partida Prevista/Real         -> atraso mediano na partida por aeroporto
"""
import glob
import itertools
import os
import unicodedata
from collections import deque
import networkx as nx
import pandas as pd
from networkx.algorithms.connectivity import (build_auxiliary_node_connectivity,
                                              local_node_connectivity)
from networkx.algorithms.flow import build_residual_network, edmonds_karp

__all__ = [
    "PREFIXOS_BR",
    "normaliza", "le_csv", "acha_coluna", "carrega_voos", "filtra_domesticos",
    "monta_grafo", "metricas_operacionais",
    "simula_falhas", "conectividade_pares", "vazao_max", "rotas_fluxo_max"
]

# Prefixos ICAO usados pelo Brasil
PREFIXOS_BR = ("SB", "SD", "SI", "SJ", "SN", "SS", "SW")


# ---------------------------------------------------------------- leitura ---
def normaliza(txt):
    """Remove acentos, espaços nas pontas e passa para minúsculas."""
    txt = unicodedata.normalize("NFKD", str(txt)).encode("ascii", "ignore").decode()
    return txt.strip().lower()


def le_csv(caminho):
    """Lê um CSV mensal do VRA: separador ';', UTF-8, linha 1 = 'Atualizado em: ...'."""
    try:
        df = pd.read_csv(caminho, sep=";", skiprows=1, encoding="utf-8-sig",
                         dtype=str, keep_default_na=False)
    except UnicodeDecodeError:
        raise ValueError(f"{caminho}: não está em UTF-8 (tente encoding='latin-1').")
    if df.shape[1] < 12:
        raise ValueError(f"{caminho}: formato inesperado ({df.shape[1]} colunas); "
                         "confira o separador (esperado ';').")
    return df


def acha_coluna(df, *palavras):
    """Devolve o nome da primeira coluna que contém todas as palavras (sem acento)."""
    for col in df.columns:
        if all(p in normaliza(col) for p in palavras):
            return col
    raise KeyError(f"Coluna com {palavras} não encontrada. Colunas: {list(df.columns)}")


def carrega_voos(pasta):
    """Lê todos os CSVs da pasta e devolve um DataFrame único com as colunas usadas."""
    arquivos = sorted(glob.glob(os.path.join(pasta, "*.csv")))
    if not arquivos:
        raise FileNotFoundError(f"Nenhum CSV em {pasta}")
    partes = []
    for a in arquivos:
        df = le_csv(a)
        partes.append(pd.DataFrame({
            "empresa": df[acha_coluna(df, "empresa")].str.strip().str.upper(),
            "tipo_linha": df[acha_coluna(df, "tipo", "linha")].str.strip().str.upper(),
            "origem": df[acha_coluna(df, "origem")].str.strip().str.upper(),
            "destino": df[acha_coluna(df, "destino")].str.strip().str.upper(),
            "situacao": df[acha_coluna(df, "situacao")].str.strip().str.upper(),
            "partida_prev": pd.to_datetime(df[acha_coluna(df, "partida", "prevista")],
                                           errors="coerce"),
            "partida_real": pd.to_datetime(df[acha_coluna(df, "partida", "real")],
                                           errors="coerce"),
        }))
        print(f"  {os.path.basename(a)}: {len(df)} linhas")
    return pd.concat(partes, ignore_index=True)


def filtra_domesticos(voos, tipos=("N",)):
    """Mantém etapas entre dois aeródromos brasileiros e dos tipos de linha pedidos.
    Devolve REALIZADOS e CANCELADOS (os cancelados não viram aresta, só estatística)."""
    tipos = [t.upper() for t in tipos]
    br = voos["origem"].str.startswith(PREFIXOS_BR) & voos["destino"].str.startswith(PREFIXOS_BR)
    br &= voos["origem"] != voos["destino"]
    print("\nEtapas entre aeródromos brasileiros, por 'Código Tipo Linha':")
    print(voos[br]["tipo_linha"].value_counts().to_string())
    print("\nSituação Voo:", voos["situacao"].value_counts().to_dict())
    dom = voos[br & voos["tipo_linha"].isin(tipos)]
    print(f"\nTipos usados: {tipos} -> {len(voos)} etapas no arquivo, {len(dom)} domésticas "
          f"({(dom['situacao'] == 'REALIZADO').sum()} realizadas)")
    return dom


# ------------------------------------------------------------------ grafo ---
def monta_grafo(realizados):
    """Grafo não direcionado; peso 'voos' = nº de etapas realizadas nos dois sentidos."""
    pares = pd.Series(list(map(lambda x: tuple(sorted(x)),
                               zip(realizados["origem"], realizados["destino"]))))
    G = nx.Graph()
    for (a, b), n in pares.value_counts().items():
        G.add_edge(a, b, voos=int(n))
    return G


def metricas_operacionais(dom):
    """Estatísticas por aeroporto usando as demais colunas do VRA."""
    a = pd.concat([dom.assign(aeroporto=dom["origem"]), dom.assign(aeroporto=dom["destino"])])
    g = a.groupby("aeroporto")
    out = pd.DataFrame({
        "etapas_programadas": g.size(), # 
        "taxa_cancel_pct": g["situacao"].apply(lambda s: round(100 * (s == "CANCELADO").mean(), 2)),
        "n_empresas": g["empresa"].nunique(),
        "pct_empresa_dominante": g["empresa"].apply(
            lambda s: round(100 * s.value_counts(normalize=True).iloc[0], 1)),
    })
    saidas = dom[dom["situacao"] == "REALIZADO"].copy()
    saidas["atraso_min"] = (saidas["partida_real"] - saidas["partida_prev"]).dt.total_seconds() / 60
    out["atraso_mediano_partida_min"] = saidas.groupby("origem")["atraso_min"].median().round(1)
    return out.reset_index()


# ---------------------------------------------------------------- análises ---
def simula_falhas(G):
    """Remove cada aeroporto e mede o estrago."""
    total_voos = sum(d["voos"] for _, _, d in G.edges(data=True))
    n = G.number_of_nodes()
    linhas = []
    for v in G.nodes:
        voos_v = sum(d["voos"] for _, _, d in G.edges(v, data=True))
        H = G.copy()
        H.remove_node(v)
        comps = sorted(nx.connected_components(H), key=len, reverse=True)
        maior = len(comps[0]) if comps else 0
        pares_perdidos = (n - 1) * (n - 2) // 2 - sum(len(c) * (len(c) - 1) // 2 for c in comps)
        linhas.append({
            "aeroporto": v,
            "grau": G.degree(v),
            "voos": voos_v,
            "pct_voos_malha": round(100 * voos_v / total_voos, 2),
            "ponto_articulacao": (n - 1) - maior > 0,
            "aeroportos_isolados": (n - 1) - maior,
            "pares_sem_rota": pares_perdidos,
        })
    return pd.DataFrame(linhas).sort_values(
        ["pares_sem_rota", "voos"], ascending=False, ignore_index=True)


def conectividade_pares(G, hubs=None):
    """kappa(s,t) (Menger, vértices) vs. limite pelo grau, só para nós NÃO adjacentes."""
    nos = list(G.nodes)
    if hubs:
        nos = sorted(nos, key=G.degree, reverse=True)[:hubs]
    H = build_auxiliary_node_connectivity(G)          # construído uma vez só
    R = build_residual_network(H, "capacity")
    linhas = []
    for s, t in itertools.combinations(nos, 2):
        if G.has_edge(s, t):
            continue
        k = local_node_connectivity(G, s, t, auxiliary=H, residual=R)
        lim = min(G.degree(s), G.degree(t))
        linhas.append({"s": s, "t": t, "kappa_st": k, "limite_grau": lim,
                       "atinge_limite": k == lim})
    return pd.DataFrame(linhas)


def vazao_max(G, s, t):
    """Fluxo máximo com capacidade = nº de voos (Edmonds-Karp)."""
    return nx.maximum_flow_value(G, s, t, capacity="voos", flow_func=edmonds_karp)

def rotas_fluxo_max(G, s, t, capacidade="voos"):
    """Fluxo máximo s->t decomposto em caminhos.
    Devolve (valor, [(caminho, fluxo), ...]) com os caminhos em ordem decrescente de fluxo."""
    valor, fluxo = nx.maximum_flow(G, s, t, capacity=capacidade, flow_func=edmonds_karp)
    resto = {u: {v: f for v, f in viz.items() if f > 0} for u, viz in fluxo.items()}
    rotas = []
    while True:
        # BFS de s até t usando só arestas com fluxo restante
        pai, fila = {s: None}, deque([s])
        while fila and t not in pai:
            u = fila.popleft()
            for v in resto[u]:
                if v not in pai:
                    pai[v] = u
                    fila.append(v)
        if t not in pai:
            break
        caminho = [t]
        while caminho[-1] != s:
            caminho.append(pai[caminho[-1]])
        caminho.reverse()
        # retira do fluxo restante o gargalo do caminho
        gargalo = min(resto[u][v] for u, v in zip(caminho, caminho[1:]))
        for u, v in zip(caminho, caminho[1:]):
            resto[u][v] -= gargalo
            if resto[u][v] == 0:
                del resto[u][v]
        rotas.append((caminho, gargalo))
    return valor, sorted(rotas, key=lambda r: r[1], reverse=True)