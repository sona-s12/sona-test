import os
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
from langchain_chroma import Chroma
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
import uuid
# from core.settings import load_and_set_decrypted_env
from logging_utils import stage_log
import config
from core.settings import setup_llm_and_embeddings

# Ensure decrypted credentials are loaded into os.environ
# load_and_set_decrypted_env()

MASTER_PATH = 'data/master_leads.xlsx'
REPORT_PATH = 'data/report.xlsx'
COOLDOWN_HOURS = 5
PERSIST_DIRECTORY = 'data/chroma_store'

load_dotenv('.env')

@stage_log(2)
def get_grouped_leads():
    if not os.path.exists(MASTER_PATH):
        return {}

    df = pd.read_excel(MASTER_PATH)
    grouped = {}

    for _, row in df.iterrows():
        src = row.get('source', 'Unknown')
        val = row.get('Email Sent Count', row.get('email_count', 0))
        if pd.isna(val):
            val = 0
        lead = {
            'id': row['ID'],
            'name': row['Name'],
            'company': row['Company'],
            'email': row['Email'],
            'description': row['Description'],
            'source': src,
            'email_count': row['email_count'],
            'last_email_sent': str(row.get('Last Email Sent', ''))
        }
        grouped.setdefault(src, []).append(lead)

    return grouped


@stage_log(1)
def send_emails_to_leads(lead_ids):
    if not os.path.exists(MASTER_PATH):
        return {'success': False, 'error': 'No leads file found', 'results': []}
        
    try:
        df = pd.read_excel(MASTER_PATH)
    except Exception as e:
        return {'success': False, 'error': f'Failed to read leads file: {str(e)}', 'results': []}
        
    results = []
    sender_email = config.EMAIL_SENDER
    sender_password = config.EMAIL_PASSWORD
    
    if not sender_email or not sender_password:
        return {'success': False, 'error': 'Email settings not configured', 'results': []}
        
    now = datetime.now()
    
    # Setup LLM, embeddings, and Chroma
    try:
        llm, embeddings = setup_llm_and_embeddings()
        company_collection = Chroma(
            collection_name="company_info_store",
            persist_directory=PERSIST_DIRECTORY,
            embedding_function=embeddings,
            collection_metadata={"hnsw:space": "cosine"}
        )
    except Exception as e:
        return {'success': False, 'error': f'Failed to initialize Azure services: {str(e)}', 'results': []}

    # --- REPORT LOGIC ---
    report_columns = [
        'ID', 'Name', 'Company', 'Email', 'Description',
        'Private Link', 'Sent Date', 'Chat Summary',
        'Status (Hot/Warm/Cold/Not Responded)', 'source'
    ]
    try:
        if os.path.exists(REPORT_PATH):
            report_df = pd.read_excel(REPORT_PATH)
        else:
            report_df = pd.DataFrame(columns=report_columns)
            report_df.to_excel(REPORT_PATH, index=False)
    except Exception as e:
        return {'success': False, 'error': f'Failed to initialize report: {str(e)}', 'results': []}
    # --- END REPORT LOGIC ---
    
    has_error = False
    for idx, row in df.iterrows():
        if str(row['ID']) not in [str(i) for i in lead_ids]:
            continue
            
        try:
            last_sent = row.get('Last Email Sent', pd.NaT)
            if pd.notna(last_sent):
                last_sent_time = pd.to_datetime(last_sent)
                if now - last_sent_time < timedelta(hours=COOLDOWN_HOURS):
                    results.append({'id': row['ID'], 'status': 'cooldown'})
                    continue
                    
            # Generate private link
            lead_id = str(uuid.uuid4())
            private_link = generate_private_link(lead_id)
            
            # Prepare user info for LLM
            user_info = {
                'name': row['Name'],
                'company': row['Company'],
                'email': row['Email']
            }
            
            # Prepare email content using LLM and Chroma
            try:
                message_content = generate_email_content(company_collection, user_info, llm, embeddings, private_link)
                if not message_content:
                    results.append({
                        'id': row['ID'],
                        'status': 'no_content',
                        'error': 'Failed to generate email content'
                    })
                    has_error = True
                    continue
            except Exception as e:
                results.append({
                    'id': row['ID'],
                    'status': 'llm_error',
                    'error': f'LLM error: {str(e)}'
                })
                has_error = True
                continue
                
            # Send email
            try:
                success = send_email_real(sender_email, sender_password, row['Email'], "Invitation to Chat with Caze BizConAI", message_content)
                if success:
                    # Increment email count
                    if 'Email Sent Count' in df.columns:
                        df.at[idx, 'Email Sent Count'] = int(df.at[idx, 'Email Sent Count'] or 0) + 1
                    elif 'email_count' in df.columns:
                        df.at[idx, 'email_count'] = int(df.at[idx, 'email_count'] or 0) + 1

                    df.at[idx, 'Last Email Sent'] = now
                    
                    # --- REPORT LOGIC ---
                    try:
                        # Check if lead already exists in report
                        existing_lead = report_df[report_df['Email'] == row['Email']]
                        if not existing_lead.empty:
                            # Update sent date
                            report_df.loc[report_df['Email'] == row['Email'], 'Sent Date'] = now
                        else:
                            # Add new lead to report
                            new_lead = {
                                'ID': lead_id,
                                'Name': row['Name'],
                                'Company': row['Company'],
                                'Email': row['Email'],
                                'Description': row['Description'],
                                'Private Link': private_link,
                                'Sent Date': now,
                                'Chat Summary': '',
                                'Status (Hot/Warm/Cold/Not Responded)': 'Not Responded',
                                'source': row.get('source', ''),
                                'Connected': row.get('Connected', False)
                            }
                            report_df = pd.concat([report_df, pd.DataFrame([new_lead])], ignore_index=True)
                        report_df.to_excel(REPORT_PATH, index=False)
                    except Exception as e:
                        results.append({
                            'id': row['ID'],
                            'status': 'error',
                            'error': f'Failed to update report: {str(e)}'
                        })
                        has_error = True
                        continue
                    # --- END REPORT LOGIC ---
                    
                    results.append({'id': row['ID'], 'status': 'sent'})
                else:
                    results.append({
                        'id': row['ID'],
                        'status': 'error',
                        'error': 'Failed to send email'
                    })
                    has_error = True
            except Exception as e:
                results.append({
                    'id': row['ID'],
                    'status': 'error',
                    'error': f'Email sending error: {str(e)}'
                })
                has_error = True
                
        except Exception as e:
            results.append({
                'id': row['ID'],
                'status': 'error',
                'error': f'Processing error: {str(e)}'
            })
            has_error = True
            
    try:
        df.to_excel(MASTER_PATH, index=False)
    except Exception as e:
        return {
            'success': False,
            'error': f'Failed to save leads file: {str(e)}',
            'results': results
        }
        
    return {
        'success': not has_error,
        'results': results
    }

@stage_log(1)
def generate_email_content(company_collection, user_info, llm, embeddings, product_link):
    # Query company info and user info context
    context = query_collections_for_email(company_collection, user_info, embeddings, llm)
    if not context:
        print("No context generated for email.")
        return None
    # Compose system prompt
    context_instr = (
    "Create an email message with only the content. Use the instructions below:\n"
    "- Use the user information to personalize your greeting. Everything inside '[]' should be replaced with the actual information.\n"
    "- Format: \"Hi [name]! I'm [Invent a friendly, realistic human name appropriate for the user's region, gender, or company, but do NOT say you are an AI assistant] from [provide company name]\"\n"
    "- Never say you are an AI, assistant, or bot. Always use a real-sounding human name.\n"
    "- Only discuss features/solutions mentioned in the company information.\n"
    "- Keep the content under 20 lines\n"
    "- Do not provide the regards or any best regards\n"
    "- End the message content with a prompt to click on this link to have a conversation with us, where I'll provide the link after this content message\n"
    )   
    base_instruction = (
        "You are an AI cold calling/texting assistant. Here is the context for our interaction:\n\n"
        f"{context}\n"
        "If there is << Previous Chat Summary >> in the context try to continue from the previous chat by letting them know the key points of previous chat and enquire them if they want to start fresh or continue from the previous.\n"
        "When generating the first message, use the user information provided above to personalize your introduction. If you can't find a name, use a generic greeting.\n"
    )
    system_prompt = base_instruction + context_instr
    # LLM expects a list of messages
    from langchain.schema import SystemMessage
    system_message = SystemMessage(content=system_prompt)
    response = llm.invoke([system_message])
    print("LLM response:", response)
    content = response.content if hasattr(response, 'content') else str(response)
    print("LLM content:", content)
    return content + f"\n\nClick here to chat with us: {product_link}"

@stage_log(2)
def query_collections_for_email(company_collection, user_info, embeddings, llm):
    # Use a simple query for now
    query_text = "company information and user information, company information more related to the user information"
    try:
        company_results = company_collection.similarity_search_by_vector(
            embedding=embeddings.embed_query(query_text), k=1
        )
        print("Chroma company_results:", company_results)
        context = []
        if company_results and len(company_results) > 0:
            for result in company_results:
                if hasattr(result, "page_content"):
                    context.append(result.page_content)
        print("Context after Chroma:", context)
        user_context = f"Name: {user_info.get('name', '')}\nCompany: {user_info.get('company', '')}\nEmail: {user_info.get('email', '')}"
        formatted_context = "<< COMPANY INFO >>\n" + "\n".join(context) + "\n\n<<END OF COMPANY INFO>>\n\n<< USER INFO >>\n" + user_context + "\n<<END OF USER INFO>>"
        print("Formatted context:", formatted_context)
        return formatted_context
    except Exception as e:
        print(f"Error querying collections: {str(e)}")
        return None

@stage_log(1)
def send_email_real(sender_email, sender_password, recipient_email, subject, message):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject
        msg.attach(MIMEText(message, 'plain'))
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, sender_password)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Error sending email: {str(e)}")
        return False

@stage_log(3)
def get_status_for_email(email):
    status_file_path = os.path.join(os.path.dirname(REPORT_PATH), "selected_users.xlsx")
    status_col = "Status (Hot/Warm/Cold/Not Responded)"
    try:
        if os.path.exists(status_file_path):
            status_df = pd.read_excel(status_file_path)
            if status_col in status_df.columns and 'Email' in status_df.columns:
                matching_status = status_df[status_df['Email'] == email]
                if not matching_status.empty:
                    status = matching_status[status_col].iloc[0]
                    if pd.notna(status):
                        return status.strip().title()
    except Exception:
        pass
    return "Not Responded"

@stage_log(2)
def generate_private_link(user_id):
    # Debug prints
    print("PRIVATE_LINK_BASE from os.environ:", config.PRIVATE_LINK_BASE)
    print("PRIVATE_LINK_PATH from os.environ:", config.PRIVATE_LINK_PATH)
    # print("Config from get_private_link_config:", config)
    base = config.PRIVATE_LINK_BASE
    path = config.PRIVATE_LINK_PATH
    return f"{base}{path}{user_id}"
