"""Minimal Streamlit entry point — verifies the project wires together."""

import streamlit as st

from game.shared_state import SharedState

# Initialise shared state once per Streamlit session.
if "shared_state" not in st.session_state:
    st.session_state.shared_state = SharedState(
        player_ids=["ai_0", "ai_1", "ai_2"],
        initial_score=0,
        initial_send_budget=10,
    )

state: SharedState = st.session_state.shared_state

st.title("Multi-Agent Game")

st.subheader("Scores")
cols = st.columns(len(state.player_ids))
for col, pid in zip(cols, state.player_ids):
    col.metric(label=pid, value=state.get_score(pid))

st.subheader("Send a message")
with st.form("send_form"):
    sender = st.selectbox("From", state.player_ids, key="sender")
    recipient = st.selectbox("To", state.player_ids, key="recipient")
    text = st.text_input("Message")
    submitted = st.form_submit_button("Send")

if submitted:
    if not text.strip():
        st.warning("Message cannot be empty.")
    elif sender == recipient:
        st.warning("Sender and recipient must be different players.")
    else:
        try:
            state.send_message(sender, recipient, text)
            st.success(
                f"Sent! {sender} budget remaining: {state.get_send_budget(sender)}"
            )
        except RuntimeError as exc:
            st.error(str(exc))

st.subheader("Inbox")
viewer = st.selectbox("View inbox for", state.player_ids, key="inbox_viewer")
if st.button("Refresh inbox"):
    messages = state.get_my_messages(viewer)
    if messages:
        for msg in messages:
            st.write(msg)
    else:
        st.info("No messages.")
