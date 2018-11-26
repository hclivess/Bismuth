import matplotlib.pyplot as plt 
import urllib3
import json
import time
import datetime
#plotly.tools.set_credentials_file(username='raetsch', api_key='ASBzv0TdgmsjVO7lhJuZ')


def get_network_height():
    """
    Returns the network height from bismuth.online API.
    Returns False if the API was not available.
    """

    http = urllib3.PoolManager()
    try:
        chainjson = http.request('GET', 'http://bismuth.online/api/stats/latestblock')
        chain = json.loads(chainjson.data.decode('utf-8'))
        height = int(chain["height"])
        last_block_time = chain["found"]
        print("Bismuth.online API says network height is {}".format(height))
        return height, last_block_time

    except Exception as e:
        print("bismuth.online API not reachable, no actual values shown")
        return False, False
        
       

#initialise variables
block = [0]
supply = [0]
rewards_miners = [0]
rewards_dev = [0]
rewards_pos = [0]
reward_block_mining = [15]
reward_block_dev = [15]
reward_block_pos = [0]
reward = 0
reward2 = 0
dev_reward = 0
hard_fork = 856440
pos_reward = 0
dev_reward2 = 0
j = 1
blocktime = 60
milestone_blocks = 10000000
zero = False
convert_to_date = []

# Try to get network height from API
network_height, last_block_mined = get_network_height()




for i in range(1, 50000001):
    
    if i < hard_fork:
        mining_reward = 15 - ((i) / (1000000))
        reward = reward + mining_reward
        
        #every 10th block, dev reward is the amount of the actual block
        if i % 10 == 0:
            dev_reward = dev_reward + mining_reward
        
        #add plotpoint every 250k Blocks
        if i == (j * 50000):
            block.append(i / 1000000)
            supply.append((reward + dev_reward) / 1000000)
            rewards_miners.append(reward / 1000000)
            rewards_dev.append(dev_reward / 1000000)
            rewards_pos.append(0)
            reward_block_mining.append(mining_reward)
            reward_block_dev.append(dev_reward)
            reward_block_pos.append(0)
            j = j + 1
            
            
    else:
        mining_reward2 = 15 - ((i) / (1000000/2)) - 0.8
        if mining_reward2 < 0:
            mining_reward2 = 0
            zero = True
        if zero == False:
            reward_zero = i + 1
        
        reward2 = reward2 + mining_reward2
        
        
        #every block is 0.8BIS going to POS-rewards, in node-code, 8 BIS are transfered every 10th block
        pos_reward = pos_reward + 0.8
        
        #every 10th block, dev reward is the amount of the actual block
        if i % 10 == 0:
            dev_reward2 = dev_reward2 + mining_reward2
        
        #add plotpoint every 250k Blocks
        if i == (j * 50000):
            block.append(i / 1000000)
            supply.append((reward + dev_reward + reward2 + dev_reward2 + pos_reward) / 1000000)
            rewards_miners.append((reward + reward2) / 1000000)
            rewards_dev.append((dev_reward + dev_reward2) / 1000000)
            rewards_pos.append(pos_reward / 1000000)
            reward_block_mining.append(mining_reward2)
            reward_block_dev.append(dev_reward2)
            reward_block_pos.append(0.8)
            j = j + 1
        elif i == reward_zero:
            block.append(i / 1000000)
            supply.append((reward + dev_reward + reward2 + dev_reward2 + pos_reward) / 1000000)
            rewards_miners.append((reward + reward2) / 1000000)
            rewards_dev.append((dev_reward + dev_reward2) / 1000000)
            rewards_pos.append(pos_reward / 1000000)
            reward_block_mining.append(mining_reward2)
            reward_block_dev.append(dev_reward2)
            reward_block_pos.append(0.8)
        
         
        if network_height:         
            if network_height == i:
                actual_supply = (reward + dev_reward + reward2 + dev_reward2 + pos_reward)
                actual_mining_reward = mining_reward2
        else:
            actual_supply = 0
 
if last_block_mined and network_height: 
    unixtime_last_block = time.mktime(datetime.datetime.strptime(last_block_mined, "%Y/%m/%d,%H:%M:%S").timetuple())

    for i in range(1,6):
        est_time_of_block = unixtime_last_block + ((i*milestone_blocks - network_height) * blocktime)
        convert_to_date.append(datetime.datetime.utcfromtimestamp(est_time_of_block).strftime('%Y/%m/%d %H:%M:%S'))     
        i = i + 1     
    
    #estimated time, when mining reward ends
    est_time_of_block = unixtime_last_block + ((7100000 - network_height) * blocktime)    
    convert_to_date.append(datetime.datetime.utcfromtimestamp(est_time_of_block).strftime('%Y/%m/%d %H:%M:%S')) 



if last_block_mined and network_height:
#plot max-, actual- and category-supply
    plt.axvline(x=10)
    plt.axvline(x=20)
    plt.axvline(x=30)
    plt.axvline(x=40)
    plt.axvline(x=50)
    #Estimation of date block 10M is mined
    plt.text(10, 90, convert_to_date[0], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(20, 90, convert_to_date[1], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(30, 90, convert_to_date[2], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(40, 90, convert_to_date[3], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(50, 90, convert_to_date[4], fontsize=12)


    plt.plot(block, supply, color='green', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='supply') 
         
    plt.plot((network_height / 1000000), (actual_supply / 1000000), color='red', linestyle='solid', linewidth = 3, 
         marker='o', markerfacecolor='red', markersize=5, label='actual supply') 
              
    plt.plot(block, rewards_dev, color='blue', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='dev rewards')

    plt.plot(block, rewards_miners, color='purple', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='miners rewards')          
         
    plt.plot(block, rewards_pos, color='brown', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='POS rewards') 

    #actual_supply value         
    plt.annotate(int(actual_supply), xy=((network_height / 1000000), (actual_supply / 1000000)), xytext=((network_height / 1000000) + 3, (actual_supply /1000000)),
            arrowprops=dict(facecolor='black', shrink=0.05),
            )
            
    #BIS-supply after 50.000.000 blocks
    plt.annotate(int(supply[-1] * 1000000), xy=(50, supply[-1]), xytext=(43, supply[-1] + 2),
            arrowprops=dict(facecolor='black', shrink=0.05),
            )

    # setting x and y axis range 
    plt.ylim(0,100) 
    plt.xlim(0,50) 
  
    # naming the x axis 
    plt.xlabel('block * 1.000.000') 
    # naming the y axis 
    plt.ylabel('BIS * 1.000.000') 

    plt.legend(loc='upper left')
  
    # giving a title to my graph 
    plt.title('BIS-supply over blocks and estimated time, when milestone-block is mined') 
    
    #save the plot to file
    plt.savefig('graphics/supply.png', dpi=600)
  
    # function to show the plot 
    plt.show()

else:
    print("Bismuth-API wasn't reachable, no plotting possible")    

if network_height and network_height:
#plot rewards per category
    plt.axvline(x=10)
    plt.axvline(x=20)
    plt.axvline(x=30)
    plt.axvline(x=40)
    plt.axvline(x=50)
    #Estimation of date block 10M is mined
    plt.text(10, 14, convert_to_date[0], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(20, 14, convert_to_date[1], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(30, 14, convert_to_date[2], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(40, 14, convert_to_date[3], fontsize=12)
    
    #Estimation of date block 10M is mined
    plt.text(50, 14, convert_to_date[4], fontsize=12)
    
    #estimated time, when mining reward ends
    plt.text(7.1, 3, convert_to_date[5], fontsize=12)
    
    plt.plot(block, reward_block_dev, color='blue', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='dev blockreward')

    plt.plot(block, reward_block_mining, color='purple', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='miners blockreward')          
         
    plt.plot(block, reward_block_pos, color='brown', linestyle='solid', linewidth = 3, 
         marker='.', markerfacecolor='blue', markersize=4, label='POS blockreward') 

    plt.annotate(reward_zero, xy=((reward_zero / 1000000), 0), xytext=((reward_zero / 1000000), 2),
            arrowprops=dict(facecolor='black', shrink=0.05),
            )   

    plt.annotate(actual_mining_reward, xy=((network_height / 1000000), actual_mining_reward), xytext=((network_height / 1000000) + 1, (actual_mining_reward) + 1),
            arrowprops=dict(facecolor='black', shrink=0.05),
            )              

    # setting x and y axis range 
    plt.ylim(0,15) 
    plt.xlim(0,50) 
  
    # naming the x axis 
    plt.xlabel('block * 1000000') 
    # naming the y axis 
    plt.ylabel('BIS') 

    plt.legend(loc='right')
  
    # giving a title to my graph 
    plt.title('BIS-rewards per category over blocks and estimated dates, when milestones  are passed') 
    
    #save the plot to file
    plt.savefig('graphics/rewards.png', dpi=600)
  
    # function to show the plot 
    plt.show() 

else:
    print("Bismuth-API wasn't reachable, no plotting possible") 